"""
The agent loop for API models. Two modes, both from ASPIRE:

tools  The model calls one tool at a time and sees a fresh wrist-camera frame after every move
       (closed-loop visual servoing). It finishes by calling done(success, summary).
code   The model writes a program against the code-mode API (code-as-policy). It runs in the
       sandbox; the trace, errors and final frame go back to the model, which judges the result
       and, on failure, diagnoses and repairs the program. The operator resets the scene between
       attempts, because nothing resets it automatically.

The tools, their schemas and the system prompt come from the toolbox (tools.py, prompts.py), the same ones
Claude Code and Codex get over MCP. After a success the operator can accept a skill drafted from the run
(with the program, in code mode). It goes to skills/pending/ until approved with `k1-agent skills approve`.
"""
import hashlib
import json
import re
import time
from dataclasses import dataclass

from k1_agent.client import StoppedByOperator
from k1_agent.providers.base import Provider, Reply, Usage, image_part, text_part
from k1_agent.trace import Trace


@dataclass
class RunResult:
    success: bool
    summary: str
    reason: str             # done | stopped | max_turns | no_program | attempts_exhausted | no_done | timeout | error
    trace_dir: str
    usage: Usage | None = None      # tokens for the whole run
    cost_usd: float | None = None   # None when a call's model has no price in pricing.yaml
    llm_calls: int = 0
    attempts: int = 0               # code mode: programs run before the verdict (success@1 vs @k)


class ConsoleOperator:
    """The human next to the arm, through the terminal that runs the agent."""

    def ask(self, question: str) -> str:
        return input(f"\n[agent asks] {question}\n> ")

    def confirm(self, question: str) -> bool:
        return input(f"\n{question} [y/N] ").strip().lower() in ("y", "yes")

    def wait_for_reset(self, message: str):
        input(f"\n{message}\nPress Enter when ready (or Ctrl+C to quit)...")


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

    def __init__(self, provider: Provider, toolbox, trace: Trace | None = None, operator=None, max_turns: int = 40,
                 image_history: int = 3, max_tokens: int = 4096, pricing=None):
        self.provider = provider
        self.toolbox = toolbox
        self.trace = trace or toolbox.trace
        self.operator = operator or getattr(toolbox, "operator", None) or ConsoleOperator()
        self.max_turns = max_turns
        self.image_history = image_history
        self.max_tokens = max_tokens
        self.pricing = pricing
        self.messages: list[dict] = []
        self.usage = Usage()
        self.cost_usd: float | None = 0.0
        self.llm_calls = 0
        self.last_program: str | None = None    # code mode: the program of the last attempt

    # ---- shared ----

    def chat(self, system: str, tools) -> Reply:
        prune_images(self.messages, self.image_history)
        started = time.monotonic()
        reply = self.provider.chat(system, self.messages, tools, self.max_tokens)
        latency = time.monotonic() - started
        self.messages.append({"role": "assistant", "reply": reply})
        model = reply.model or self.provider.model
        cost = self.pricing.cost(model, reply.usage) if self.pricing is not None else None
        self.llm_calls += 1
        if reply.usage is not None:
            self.usage = self.usage + reply.usage
        # one unpriced call makes the run's total unknown rather than silently too low
        self.cost_usd = self.cost_usd + cost if (cost is not None and self.cost_usd is not None) else None
        self.trace.log("llm", text=reply.text, tool_calls=[{"name": c.name, "arguments": c.arguments}
                                                            for c in reply.tool_calls],
                       model=model, latency_s=round(latency, 3),
                       usage=reply.usage.as_dict() if reply.usage else None, cost_usd=cost)
        if reply.text.strip():
            self.toolbox.narrate(reply.text.strip()[:300])
        return reply

    def run_tool(self, name: str, args: dict) -> tuple[dict, bytes | None]:
        if name not in ("note", "done"):
            self.toolbox.narrate(f"-> {name}({json.dumps(args, default=str)[:200]})")
        return self.toolbox.call(name, args)

    def tool_results(self, reply: Reply) -> list[dict]:
        results = []
        for call in reply.tool_calls:
            result, frame = self.run_tool(call.name, call.arguments)
            content = [text_part(result_text(result))] + ([image_part(frame)] if frame else [])
            results.append({"id": call.id, "name": call.name, "content": content})
        return results

    def finish(self, result: RunResult) -> RunResult:
        result.usage, result.cost_usd, result.llm_calls = self.usage, self.cost_usd, self.llm_calls
        self.trace.log("outcome", success=result.success, summary=result.summary, reason=result.reason,
                       usage=self.usage.as_dict(), cost_usd=self.cost_usd, llm_calls=self.llm_calls,
                       attempts=result.attempts)
        self.toolbox.narrate(f"{'SUCCESS' if result.success else 'FAILED'} ({result.reason}): {result.summary[:200]}")
        return result

    # ---- mode: tools ----

    def run_tools(self, task: str) -> RunResult:
        try:
            system, observation = self.toolbox.begin()
            tools = self.toolbox.specs()
            self.trace.log("task", task=task, mode="tools", provider=self.provider.name, model=self.provider.model,
                           profile=self.toolbox.profile)
            self.messages = [{"role": "user", "content": [text_part(f"Task: {task}")] + observation}]
            self.toolbox.outcome = None
            for _ in range(self.max_turns):
                reply = self.chat(system, tools)
                if not reply.tool_calls:
                    self.messages.append({"role": "user", "content": [text_part(
                        "Continue with a tool call, or call done(success, summary) if you are finished.")]})
                    continue
                self.messages.append({"role": "tool", "results": self.tool_results(reply)})
                if self.toolbox.outcome is not None:
                    success, summary = self.toolbox.outcome
                    return self.finish(RunResult(success, summary, "done", self.trace.dir))
            return self.finish(RunResult(False, f"no result after {self.max_turns} turns", "max_turns", self.trace.dir))
        except StoppedByOperator:
            return self.finish(RunResult(False, "the operator pressed STOP", "stopped", self.trace.dir))

    # ---- mode: code (the arm only) ----

    def _get_program(self, system: str) -> str | None:
        reading = self.toolbox.specs(only=["read_skill", "list_skills"])
        for _ in range(6):
            reply = self.chat(system, reading)
            if reply.tool_calls:
                self.messages.append({"role": "tool", "results": self.tool_results(reply)})
                continue
            code = extract_code(reply.text)
            if code:
                return code
            self.messages.append({"role": "user", "content": [text_part(
                "Reply with the complete program in a single ```python block.")]})
        return None

    def _get_verdict(self, system: str) -> tuple[bool, str]:
        self.toolbox.outcome = None
        for _ in range(3):
            reply = self.chat(system, self.toolbox.specs(only=["done"]))
            if reply.tool_calls:
                self.messages.append({"role": "tool", "results": self.tool_results(reply)})
            if self.toolbox.outcome is not None:
                return self.toolbox.outcome
            self.messages.append({"role": "user", "content": [text_part("Call done(success, summary).")]})
        return False, "the model gave no verdict"

    def run_code(self, task: str, attempts: int = 3) -> RunResult:
        from k1_agent.prompts import CODE_INSTRUCTIONS, api_doc

        box = self.toolbox
        try:
            system, observation = box.begin()
            self.trace.log("task", task=task, mode="code", provider=self.provider.name, model=self.provider.model,
                           profile=box.profile)
            self.messages = [{"role": "user", "content": [text_part(f"Task: {task}")] + observation +
                              [text_part(CODE_INSTRUCTIONS.format(api=api_doc(box)))]}]
            summary = ""
            for attempt in range(1, attempts + 1):
                code = self._get_program(system)
                if code is None:
                    return self.finish(RunResult(False, "the model did not produce a program", "no_program",
                                                 self.trace.dir, attempts=attempt - 1))
                self.last_program = code
                self.trace.save_text(f"program_{attempt}.py", code)
                self.trace.log("program", attempt=attempt, code=code)
                box.narrate(f"running program attempt {attempt}")
                start = len(self.trace.entries)
                result = box.run_program(code)
                calls = self.trace.calls_since(start)
                self.trace.log("program_result", attempt=attempt, ok=result.ok, error=result.error,
                               returned=result.returned, output=result.output)

                report = [f"Attempt {attempt} finished. Program {'completed' if result.ok else 'FAILED'}.",
                          f"error: {result.error}" if result.error else f"returned: {json.dumps(result.returned, default=str)}",
                          "printed:\n" + "\n".join(result.output[-30:]) if result.output else "printed: nothing",
                          "primitive calls (trace):\n" + Trace.summarize(calls)]
                after, frame = box.call("observe", {})
                self.messages.append({"role": "user", "content": [text_part("\n\n".join(report))] +
                                      observation_parts(after, frame, "Observation after the program:") +
                                      [text_part("Look at the frame. Did the task succeed? Call done(success, summary).")]})
                success, summary = self._get_verdict(system)
                if success:
                    return self.finish(RunResult(True, summary, "done", self.trace.dir, attempts=attempt))
                if attempt < attempts:
                    self.operator.wait_for_reset(f"Attempt {attempt} failed: {summary}\nReset the scene for attempt "
                                                 f"{attempt + 1}.")
                    after, frame = box.call("observe", {})
                    self.messages.append({"role": "user", "content": [text_part(
                        "The operator reset the scene. Diagnose what went wrong from the trace and frames, then write "
                        "a repaired program in one ```python block.")] +
                        observation_parts(after, frame, "Observation after the reset:")})
            return self.finish(RunResult(False, summary, "attempts_exhausted", self.trace.dir, attempts=attempts))
        except StoppedByOperator:
            return self.finish(RunResult(False, "the operator pressed STOP", "stopped", self.trace.dir))

    # ---- skills ----

    def draft_skill(self, task: str) -> str | None:
        """Ask the model to turn this successful run into a skill note; saved to skills/pending/, with the
        program as a code skill when the run was in code mode."""
        self.messages.append({"role": "user", "content": [text_part(
            "The task succeeded. Write a short skill note for the library so a future run can repeat this. Format:\n"
            "---\nname: <lowercase-with-dashes>\ndescription: <one line: when to use it>\n---\n"
            "<what worked: joint values or ranges, the order of moves, what to check in the camera, pitfalls>\n"
            f"Task was: {task}")]})
        reply = self.chat("You write concise, factual robot skill notes.", [])
        text = reply.text.strip()
        match = re.search(r"^name:\s*([a-z0-9][a-z0-9_-]*)", text, re.M)
        name = match.group(1) if match else re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")[:40]
        skills = self.toolbox.skills
        try:
            path = skills.save_draft(name, text)
            if self.last_program:
                skills.save_code_draft(name, self.last_program)
            return path
        except ValueError:
            return None


def run_frozen_program(toolbox, name: str, code: str) -> RunResult:
    """A program does the whole task with no model. It gives its verdict by returning {"success": bool,
    "summary": str}; anything else counts as no verdict."""
    trace = toolbox.trace
    trace.log("program", model=f"{name}@{hashlib.sha1(code.encode()).hexdigest()[:10]}", code=code)
    try:
        result = toolbox.run_program(code)
    except StoppedByOperator:
        return RunResult(False, "the operator pressed STOP", "stopped", trace.dir, cost_usd=0.0)
    trace.log("program_result", ok=result.ok, error=result.error, returned=result.returned, output=result.output)
    verdict = result.returned if isinstance(result.returned, dict) else {}
    if not result.ok:
        reason = "timeout" if (result.error or "").startswith("timed out") else "error"
        outcome = RunResult(False, result.error or "", reason, trace.dir, cost_usd=0.0, attempts=1)
    elif isinstance(verdict.get("success"), bool) and isinstance(verdict.get("summary"), str):
        outcome = RunResult(verdict["success"], verdict["summary"], "done", trace.dir, cost_usd=0.0, attempts=1)
    else:
        outcome = RunResult(False, "the program gave no verdict", "no_done", trace.dir, cost_usd=0.0, attempts=1)
    trace.log("outcome", success=outcome.success, summary=outcome.summary, reason=outcome.reason, cost_usd=0.0,
              llm_calls=0, attempts=1)
    return outcome


def observation_parts(obs: dict, frame: bytes | None, caption: str) -> list[dict]:
    parts = [text_part(f"{caption}\n{result_text(obs)}")]
    parts.append(image_part(frame) if frame else text_part("(no camera frame available)"))
    return parts
