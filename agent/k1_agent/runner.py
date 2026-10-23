"""
The agent loop. Two modes, both from ASPIRE:

tools  The model calls one primitive at a time and sees a fresh wrist-camera frame after every
       move (closed-loop visual servoing). It finishes by calling done(success, summary).
code   The model writes a program against the primitive API (code-as-policy). It runs in the
       sandbox; the trace, errors and final frame go back to the model, which judges the result
       and, on failure, diagnoses and repairs the program. The operator resets the scene between
       attempts, because nothing resets it automatically.

After a success the operator can accept a new skill drafted from the run. It goes to
skills/pending/ until approved with `k1-agent skills approve`.
"""
import inspect
import json
import re
from dataclasses import dataclass

from k1_agent.client import StoppedByOperator
from k1_agent.primitives import ARM_JOINTS, PROGRAM_API, Primitives
from k1_agent.providers.base import Provider, Reply, ToolSpec, image_part, text_part
from k1_agent.sandbox import run_program
from k1_agent.skills import SkillLibrary
from k1_agent.trace import Trace

NUMBER = {"type": "number"}
TIMEOUT = {"type": "number", "description": "seconds to wait for the move to finish (default 10)"}

PRIMITIVE_TOOLS = [
    ToolSpec("observe", "Get the arm state (joint angles in rad, gripper, limits, saved poses) and a fresh "
             "wrist-camera image.", {"type": "object", "properties": {}}),
    ToolSpec("move_joints", "Move to absolute joint angles in radians, smoothly, and wait until reached. Give all "
             f"6 angles in the order {ARM_JOINTS}, or an object with only the joints to change.",
             {"type": "object", "properties": {
                 "positions": {"anyOf": [{"type": "array", "items": NUMBER, "minItems": 6, "maxItems": 6},
                                         {"type": "object", "additionalProperties": NUMBER}]},
                 "timeout": TIMEOUT}, "required": ["positions"]}),
    ToolSpec("move_joint_delta", "Move one joint by a small relative amount in radians (capped per call).",
             {"type": "object", "properties": {"joint": {"type": "string", "enum": ARM_JOINTS},
                                               "delta": NUMBER, "timeout": TIMEOUT},
              "required": ["joint", "delta"]}),
    ToolSpec("go_pose", "Move to a saved, named pose.",
             {"type": "object", "properties": {"name": {"type": "string"}, "timeout": TIMEOUT}, "required": ["name"]}),
    ToolSpec("go_home", "Move to the 'home' pose.", {"type": "object", "properties": {}}),
    ToolSpec("set_gripper", "Set the gripper: 1.0 fully open, 0.0 closed. Values in between partly close it.",
             {"type": "object", "properties": {"value": {"type": "number", "minimum": 0, "maximum": 1}},
              "required": ["value"]}),
    ToolSpec("stop", "Hold the current position immediately.", {"type": "object", "properties": {}}),
    ToolSpec("wait", "Pause for up to 10 seconds.",
             {"type": "object", "properties": {"seconds": NUMBER}, "required": ["seconds"]}),
]
ENABLE_TOOL = ToolSpec("enable", "Lock (true) or release (false) the motors. Releasing makes the arm fall.",
                       {"type": "object", "properties": {"value": {"type": "boolean"}}, "required": ["value"]})
READ_SKILL = ToolSpec("read_skill", "Read the full text of a skill from the skill library.",
                      {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})
ASK_OPERATOR = ToolSpec("ask_operator", "Ask the human operator a question (for example to place or reset an "
                        "object, or when unsure if something is safe). Returns their answer.",
                        {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]})
DONE = ToolSpec("done", "Finish the task. Only claim success if the latest camera frame shows it.",
                {"type": "object", "properties": {"success": {"type": "boolean"}, "summary": {"type": "string"}},
                 "required": ["success", "summary"]})
MOTION_TOOLS = {"move_joints", "move_joint_delta", "go_pose", "go_home", "set_gripper"}

SYSTEM_PROMPT = """You control a real Unitree K1 robot arm through a small set of primitives.

The arm:
- 6 arm joints, in radians, in this order: {joints}. Plus a gripper: 1.0 is fully open, 0.0 is closed.
- Joint limits for this setup: {limits}
- Saved poses: {poses}
- The only camera is on the wrist, next to the gripper. Each image is what the gripper "sees". There is no
  depth sensor, so judge distance from apparent size and from how the view changes as you move.

Safety (the server also enforces limits and speed, but be careful anyway):
- Move in small steps when near objects or the table. Prefer move_joint_delta steps of 0.05 to 0.15 rad and
  look again after each step.
- After every move you get a fresh frame. Check it before the next move. If something looks wrong, call stop.
- If the operator presses STOP the task ends immediately.
- Never claim success unless the latest frame shows it. If you are unsure, ask_operator.

Skill library (read the relevant ones with read_skill before starting):
{skills}
"""

CODE_INSTRUCTIONS = """Write a Python program that does the task. Define `run(k1)`; it may only use these
primitives on `k1` (and `import math`). Every call returns a dict with "ok"; check it and react to failures
(fields "error" and "message"). observe() returns joints and state; it cannot show you the image inside the
program, so structure the program as moves plus checks of joint values, and you will see the final frame after
it runs.

{api}

Reply with a short plan, then the whole program in one ```python block."""


@dataclass
class RunResult:
    success: bool
    summary: str
    reason: str             # done | stopped | max_turns | no_program | attempts_exhausted
    trace_dir: str


class ConsoleOperator:
    """The human next to the arm, through the terminal that runs the agent."""

    def ask(self, question: str) -> str:
        return input(f"\n[agent asks] {question}\n> ")

    def confirm(self, question: str) -> bool:
        return input(f"\n{question} [y/N] ").strip().lower() in ("y", "yes")

    def wait_for_reset(self, message: str):
        input(f"\n{message}\nPress Enter when ready (or Ctrl+C to quit)...")


def api_doc() -> str:
    lines = []
    for name in PROGRAM_API:
        fn = getattr(Primitives, name)
        params = [p for p in inspect.signature(fn).parameters.values() if p.name != "self"]
        doc = (inspect.getdoc(fn) or "").splitlines()[0] if inspect.getdoc(fn) else ""
        lines.append(f"- k1.{name}({', '.join(str(p) for p in params)}): {doc}")
    return "\n".join(lines)


def extract_code(text: str) -> str | None:
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text or "", re.S)
    return max(blocks, key=len).strip() if blocks else None


THINKING_TYPES = {"thinking", "redacted_thinking"}


def prune_images(messages: list[dict], keep: int):
    """Keep only the newest `keep` camera frames in the conversation, to bound tokens.

    Claude's thinking blocks are signed against everything before them, so once an earlier frame
    is replaced, the thinking blocks of later assistant turns are dropped (the API accepts that)."""
    seen, first_changed = 0, None
    for index in reversed(range(len(messages))):
        msg = messages[index]
        groups = [msg["content"]] if msg["role"] == "user" else (
            [r["content"] for r in msg["results"]] if msg["role"] == "tool" else [])
        for parts in groups:
            for i in reversed(range(len(parts))):
                if parts[i]["type"] == "image":
                    seen += 1
                    if seen > keep:
                        parts[i] = text_part("[older camera frame omitted]")
                        first_changed = index
    if first_changed is None:
        return
    for msg in messages[first_changed + 1:]:
        if msg["role"] == "assistant":
            raw = msg["reply"].raw
            kept = [b for b in raw if not (isinstance(b, dict) and b.get("type") in THINKING_TYPES)]
            if len(kept) != len(raw):
                msg["reply"].raw = kept or [{"type": "text", "text": msg["reply"].text or "(thinking omitted)"}]


def result_text(result: dict) -> str:
    return json.dumps({k: v for k, v in result.items() if k != "image"}, default=str)


class AgentRunner:

    def __init__(self, provider: Provider, prims: Primitives, skills: SkillLibrary, trace: Trace,
                 operator=None, max_turns: int = 40, image_history: int = 3, max_tokens: int = 4096,
                 program_timeout: float = 180.0):
        self.provider = provider
        self.prims = prims
        self.skills = skills
        self.trace = trace
        self.operator = operator or ConsoleOperator()
        self.max_turns = max_turns
        self.image_history = image_history
        self.max_tokens = max_tokens
        self.program_timeout = program_timeout
        self.messages: list[dict] = []

    # ---- shared ----

    def system_prompt(self, obs: dict) -> str:
        limits = ", ".join(f"{n} [{a:.2f}, {b:.2f}]" for n, (a, b) in obs.get("limits", {}).items())
        return SYSTEM_PROMPT.format(joints=", ".join(ARM_JOINTS), limits=limits or "unknown",
                                    poses=", ".join(obs.get("poses", [])) or "none", skills=self.skills.index_text())

    def observation_parts(self, obs: dict, caption: str) -> list[dict]:
        parts = [text_part(f"{caption}\n{result_text(obs)}")]
        if obs.get("image"):
            parts.append(image_part(obs["image"]))
        else:
            parts.append(text_part("(no camera frame available)"))
        return parts

    def chat(self, system: str, tools: list[ToolSpec]) -> Reply:
        prune_images(self.messages, self.image_history)
        reply = self.provider.chat(system, self.messages, tools, self.max_tokens)
        self.messages.append({"role": "assistant", "reply": reply})
        self.trace.log("llm", text=reply.text, tool_calls=[{"name": c.name, "arguments": c.arguments}
                                                            for c in reply.tool_calls])
        if reply.text.strip():
            self.prims.note(reply.text.strip()[:300])
        return reply

    def run_tool(self, name: str, args: dict) -> dict:
        if name == "read_skill":
            text = self.skills.read(args.get("name", ""))
            return {"ok": text is not None, "skill": text} if text else {
                "ok": False, "error": "unknown_skill", "message": f"skills: {[s['name'] for s in self.skills.list()]}"}
        if name == "ask_operator":
            answer = self.operator.ask(str(args.get("question", "")))
            self.trace.log("operator", question=args.get("question"), answer=answer)
            return {"ok": True, "answer": answer}
        if name in {t.name for t in PRIMITIVE_TOOLS} or name == "enable":
            self.prims.note(f"-> {name}({json.dumps(args)[:200]})")
            try:
                return getattr(self.prims, name)(**args)
            except TypeError as e:
                return {"ok": False, "error": "bad_arguments", "message": str(e)}
        return {"ok": False, "error": "unknown_tool", "message": f"no tool {name!r}"}

    def finish(self, result: RunResult) -> RunResult:
        self.trace.log("outcome", success=result.success, summary=result.summary, reason=result.reason)
        self.prims.note(f"{'SUCCESS' if result.success else 'FAILED'} ({result.reason}): {result.summary[:200]}")
        return result

    # ---- mode: tools ----

    def run_tools(self, task: str) -> RunResult:
        try:
            obs = self.prims.observe()
            system = self.system_prompt(obs)
            tools = PRIMITIVE_TOOLS + ([ENABLE_TOOL] if self.prims.allow_enable else []) + \
                [READ_SKILL, ASK_OPERATOR, DONE]
            self.trace.log("task", task=task, mode="tools", provider=self.provider.name, model=self.provider.model)
            self.messages = [{"role": "user", "content": [text_part(f"Task: {task}")] +
                              self.observation_parts(obs, "Current observation:")}]
            for _ in range(self.max_turns):
                reply = self.chat(system, tools)
                if not reply.tool_calls:
                    self.messages.append({"role": "user", "content": [text_part(
                        "Continue with a tool call, or call done(success, summary) if you are finished.")]})
                    continue
                results, outcome = [], None
                for call in reply.tool_calls:
                    if call.name == "done":
                        outcome = RunResult(bool(call.arguments.get("success")),
                                            str(call.arguments.get("summary", "")), "done", self.trace.dir)
                        results.append({"id": call.id, "name": call.name, "content": [text_part('{"ok": true}')]})
                        continue
                    result = self.run_tool(call.name, call.arguments)
                    content = [text_part(result_text(result))]
                    frame = result.get("image") if call.name == "observe" else (
                        self.prims.last_frame if call.name in MOTION_TOOLS else None)
                    if frame:
                        content.append(image_part(frame))
                    results.append({"id": call.id, "name": call.name, "content": content})
                self.messages.append({"role": "tool", "results": results})
                if outcome:
                    return self.finish(outcome)
            return self.finish(RunResult(False, f"no result after {self.max_turns} turns", "max_turns", self.trace.dir))
        except StoppedByOperator:
            return self.finish(RunResult(False, "the operator pressed STOP", "stopped", self.trace.dir))

    # ---- mode: code ----

    def _get_program(self, system: str) -> str | None:
        for _ in range(6):
            reply = self.chat(system, [READ_SKILL])
            if reply.tool_calls:
                self.messages.append({"role": "tool", "results": [
                    {"id": c.id, "name": c.name, "content": [text_part(result_text(self.run_tool(c.name, c.arguments)))]}
                    for c in reply.tool_calls]})
                continue
            code = extract_code(reply.text)
            if code:
                return code
            self.messages.append({"role": "user", "content": [text_part(
                "Reply with the complete program in a single ```python block.")]})
        return None

    def _get_verdict(self, system: str) -> tuple[bool, str]:
        for _ in range(3):
            reply = self.chat(system, [DONE])
            done = next((c for c in reply.tool_calls if c.name == "done"), None)
            if reply.tool_calls:
                self.messages.append({"role": "tool", "results": [
                    {"id": c.id, "name": c.name, "content": [text_part('{"ok": true}')]} for c in reply.tool_calls]})
            if done:
                return bool(done.arguments.get("success")), str(done.arguments.get("summary", ""))
            self.messages.append({"role": "user", "content": [text_part("Call done(success, summary).")]})
        return False, "the model gave no verdict"

    def run_code(self, task: str, attempts: int = 3) -> RunResult:
        try:
            obs = self.prims.observe()
            system = self.system_prompt(obs)
            self.trace.log("task", task=task, mode="code", provider=self.provider.name, model=self.provider.model)
            self.messages = [{"role": "user", "content": [text_part(f"Task: {task}")] +
                              self.observation_parts(obs, "Current observation:") +
                              [text_part(CODE_INSTRUCTIONS.format(api=api_doc()))]}]

            def dispatch(name, args, kwargs):
                return getattr(self.prims, name)(*args, **kwargs)

            summary = ""
            for attempt in range(1, attempts + 1):
                code = self._get_program(system)
                if code is None:
                    return self.finish(RunResult(False, "the model did not produce a program", "no_program",
                                                 self.trace.dir))
                self.trace.save_text(f"program_{attempt}.py", code)
                self.trace.log("program", attempt=attempt, code=code)
                self.prims.note(f"running program attempt {attempt}")
                start = len(self.trace.entries)
                result = run_program(code, dispatch, PROGRAM_API, timeout=self.program_timeout)
                calls = self.trace.calls_since(start)
                self.trace.log("program_result", attempt=attempt, ok=result.ok, error=result.error,
                               returned=result.returned, output=result.output)

                report = [f"Attempt {attempt} finished. Program {'completed' if result.ok else 'FAILED'}.",
                          f"error: {result.error}" if result.error else f"returned: {json.dumps(result.returned, default=str)}",
                          "printed:\n" + "\n".join(result.output[-30:]) if result.output else "printed: nothing",
                          "primitive calls (trace):\n" + Trace.summarize(calls)]
                obs = self.prims.observe()
                self.messages.append({"role": "user", "content": [text_part("\n\n".join(report))] +
                                      self.observation_parts(obs, "Observation after the program:") +
                                      [text_part("Look at the frame. Did the task succeed? Call done(success, summary).")]})
                success, summary = self._get_verdict(system)
                if success:
                    return self.finish(RunResult(True, summary, "done", self.trace.dir))
                if attempt < attempts:
                    self.operator.wait_for_reset(f"Attempt {attempt} failed: {summary}\nReset the scene for attempt "
                                                 f"{attempt + 1}.")
                    obs = self.prims.observe()
                    self.messages.append({"role": "user", "content": [text_part(
                        "The operator reset the scene. Diagnose what went wrong from the trace and frames, then write "
                        "a repaired program in one ```python block.")] +
                        self.observation_parts(obs, "Observation after the reset:")})
            return self.finish(RunResult(False, summary, "attempts_exhausted", self.trace.dir))
        except StoppedByOperator:
            return self.finish(RunResult(False, "the operator pressed STOP", "stopped", self.trace.dir))

    # ---- skills ----

    def draft_skill(self, task: str) -> str | None:
        """Ask the model to turn this successful run into a skill note; saved to skills/pending/."""
        self.messages.append({"role": "user", "content": [text_part(
            "The task succeeded. Write a short skill note for the library so a future run can repeat this. Format:\n"
            "---\nname: <lowercase-with-dashes>\ndescription: <one line: when to use it>\n---\n"
            "<what worked: joint values or ranges, the order of moves, what to check in the camera, pitfalls>\n"
            f"Task was: {task}")]})
        reply = self.chat("You write concise, factual robot skill notes.", [])
        text = reply.text.strip()
        match = re.search(r"^name:\s*([a-z0-9][a-z0-9_-]*)", text, re.M)
        name = match.group(1) if match else re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")[:40]
        try:
            return self.skills.save_draft(name, text)
        except ValueError:
            return None
