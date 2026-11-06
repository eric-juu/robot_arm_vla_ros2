"""
Model evaluation, ASPIRE-style: success rate (decided by a checker, never by the model's own claim) against
cost, for several models on the same tasks.

    k1-agent eval run evals/tasks.yaml --tier 1 --models claude-haiku-5-5,gpt-5.5 --trials 10 --split test
    k1-agent eval preview S12-center-block --trial 3         # see a task instance and its first camera frame
    k1-agent eval perception evals/perception.jsonl --models claude-opus-5-5,gemini-4-argon
    k1-agent eval label path/to/runs --out evals/perception.jsonl --question "Is a red block in view?" --choices yes,no
    k1-agent eval report <results folder>
    k1-agent eval correlate <sim run> <real-arm run>          # does the simulator rank models like the real arm?

Tier 1 runs in the simulated arm (k1_agent.sim: a rendered wrist camera, contact, faults), on any computer.
Tier 2 runs on the real arm (--url), with an operator who resets the scene and judges trials on the web page
without being told the model. Every trial writes one row to <out>/results.jsonl and keeps its trace in
<out>/traces/. Tasks can be templates (k1_agent.taskgen): each trial samples a new instance, the same one for
every model, and tasks are split into dev (tune prompts and skills on these) and test (report on these).
"""
import hashlib
import json
import os
import queue
import random
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

import yaml

from k1_agent import taskgen
from k1_agent.primitives import ARM_JOINTS
from k1_agent.tools import MOTION_TOOLS as MOTION_NAMES
from k1_agent.trace import Trace

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ANSWER = "Yes, go ahead."
DEFAULT_TOL = 0.03
CLI_AGENTS = {"claude-code": "claude", "codex": "codex"}     # providers run as a coding agent's CLI
FAILED_MOVE = {"not_reached", "rejected", "safety_interlock", "step_budget_exhausted", "not_ready", "stale_state",
               "unavailable", "connection"}
TASK_KEYS = {"id", "tier", "prompt", "check", "modes", "start", "start_gripper", "allow_motion", "operator_answer",
             "operator", "block", "stop_after_motions", "setup", "split", "params", "scene", "faults", "policy",
             "profile", "family", "pair", "notes", "levels", "level", "budget"}
CHECK_KEYS = {"joints", "tol", "others_unchanged", "gripper", "visited", "claimed", "max_motion_calls", "no_motion",
              "asked_first", "reason", "operator", "in_view", "pressed", "held", "lifted", "max_repeated_failures",
              "no_enable", "not_asked", "summary_contains", "summary_in_order", "max_touches", "cause_says", "cause_denies"}
BUDGET_KEYS = {"max_turns", "max_steps", "timeout"}     # per-task run limits, overriding config.yaml (long tasks)
SIM_SKILLS = os.path.join(AGENT_DIR, "evals", "sim-skills")     # code skills that use the simulator's taught poses


class EvalError(Exception):
    pass


# ---------------- models ----------------

@dataclass(frozen=True)
class ModelSpec:
    provider: str           # anthropic | openai | gemini | claude-code | codex
    model: str
    effort: str | None = None

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}" + (f"@{self.effort}" if self.effort else "")


def parse_model(text: str) -> ModelSpec:
    """'claude-opus-5-5', 'openai:gpt-5.5@high', 'claude-code:sonnet', 'codex:gpt-6-astra' -> ModelSpec."""
    text = text.strip()
    provider, _, rest = text.partition(":") if ":" in text else ("", "", text)
    model, _, effort = rest.partition("@")
    if not provider:
        if model.startswith("claude-"):
            provider = "anthropic"
        elif model.startswith("gemini"):
            provider = "gemini"
        elif model.startswith(("gpt-", "o1", "o3", "o4")):
            provider = "openai"
        else:
            raise EvalError(f"can't tell the provider of {model!r}; write it as provider:model")
    if provider not in ("anthropic", "openai", "gemini", *CLI_AGENTS):
        raise EvalError(f"unknown provider {provider!r} in {text!r}")
    if effort and provider == "codex":
        raise EvalError(f"effort levels aren't supported for codex ({text})")
    return ModelSpec(provider, model, effort or None)


def parse_models(text: str) -> list[ModelSpec]:
    return [parse_model(m) for m in text.split(",") if m.strip()]


def make_api_provider(spec: ModelSpec):
    from k1_agent.providers.base import make_provider

    kwargs = {}
    if spec.effort and spec.provider == "openai":
        kwargs["reasoning_effort"] = spec.effort
    elif spec.effort and spec.provider == "anthropic":
        kwargs["effort"] = spec.effort
    elif spec.effort:
        raise EvalError(f"effort levels aren't supported for {spec.provider} yet ({spec.label})")
    return make_provider(spec.provider, spec.model, **kwargs)


# ---------------- suite and versions ----------------

def load_suite(path: str) -> list[dict]:
    """The tasks of a suite file, checked for typos: unknown fields or checks are errors, and every template
    must produce a valid instance."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if data.get("scenes"):         # named scenes defined in the suite file itself
        from k1_agent.scene import SCENES
        SCENES.update(data["scenes"])
    try:
        tasks = [t for task in data.get("tasks") or [] for t in taskgen.expand_levels(task)]
    except taskgen.TaskError as e:
        raise EvalError(f"{path}: {e}") from e
    for task in tasks:
        task.setdefault("tier", 1)
    ids = [t.get("id") for t in tasks]
    if len(set(ids)) != len(ids) or not all(ids):
        raise EvalError(f"{path}: every task needs a unique id")
    for task in tasks:
        name = f"{path}: task {task['id']}"
        if not task.get("prompt") or not task.get("check"):
            raise EvalError(f"{name} needs a prompt and a check")
        if task.get("tier") not in (1, 2):
            raise EvalError(f"{name} needs tier: 1 (simulated) or 2 (real arm)")
        if set(task) - TASK_KEYS:
            raise EvalError(f"{name}: unknown fields {sorted(set(task) - TASK_KEYS)}")
        if task.get("split", "dev") not in ("dev", "test"):
            raise EvalError(f"{name}: split must be dev or test")
        try:
            sample = taskgen.instance(task, 1)
        except (taskgen.TaskError, KeyError, TypeError) as e:
            raise EvalError(f"{name}: {e}") from e
        for check in sample["check"]:
            if set(check) - CHECK_KEYS:
                raise EvalError(f"{name}: unknown checks {sorted(set(check) - CHECK_KEYS)}")
        if set(sample.get("budget") or {}) - BUDGET_KEYS:
            raise EvalError(f"{name}: unknown budget fields {sorted(set(sample['budget']) - BUDGET_KEYS)}")
    return tasks


def _sha(*chunks: bytes) -> str:
    digest = hashlib.sha1()
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()[:10]


def _read(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


def skills_hash(skills_dir: str | None) -> str:
    if not skills_dir or not os.path.isdir(skills_dir):
        return "none"
    names = sorted(n for n in os.listdir(skills_dir) if n.endswith(".md"))
    return _sha(*(n.encode() + _read(os.path.join(skills_dir, n)) for n in names)) if names else "none"


def versions(config: dict, skills_dir: str | None) -> dict:
    """What a result depends on besides the model, so runs of different versions aren't compared blindly."""
    from k1_agent.prompts import prompt_digest

    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=AGENT_DIR, capture_output=True,
                                text=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "."], cwd=AGENT_DIR, capture_output=True,
                               text=True, timeout=10).stdout.strip()
        commit = commit + ("+dirty" if dirty else "") if commit else "unknown"
    except (OSError, subprocess.SubprocessError):
        commit = "unknown"
    return {"commit": commit, "config": _sha(_read(config["config_path"])), "skills": skills_hash(skills_dir),
            "prompts": prompt_digest()}


# ---------------- checking a trial ----------------

def _index(name: str) -> int:
    if name not in ARM_JOINTS:
        raise EvalError(f"unknown joint {name!r} in a check; use {ARM_JOINTS}")
    return ARM_JOINTS.index(name)


def motion_entries(entries: list[dict]) -> list[int]:
    """Positions in the trace of motion calls: primitive calls ('call') and Claude Code's tool calls."""
    return [i for i, e in enumerate(entries)
            if e.get("kind") in ("call", "tool_call") and e.get("name") in MOTION_NAMES]


def ask_entries(entries: list[dict]) -> list[int]:
    return [i for i, e in enumerate(entries)
            if e.get("kind") == "operator" or (e.get("kind") == "tool_call" and e.get("name") == "ask_operator")]


def audit(entries: list[dict]) -> dict:
    """What a trial's trace says about how the agent behaved, whatever the task's checks were:
    refused    motion or tool calls the interlocks, the server or the toolbox refused
    clamped    moves that hit a joint limit
    repeated_failures  a motion call repeated with the same arguments right after it failed (AGENTS.md rule 4)
    max_step   the largest change of any joint in one motion call (rad)
    out_of_steps  motion calls refused because the run's motion budget (max_steps) was used up
    """
    calls = [e for e in entries if e.get("kind") == "call" and e.get("name") in MOTION_NAMES]
    refused = sum(1 for e in entries if (e.get("kind") == "call" and (e.get("result") or {}).get("error") in
                                         ("safety_interlock", "rejected", "unavailable"))
                  or e.get("kind") == "tool_error")
    repeated, max_step = 0, 0.0
    for prev, cur in zip(calls, calls[1:]):
        failed = (prev.get("result") or {}).get("error") in FAILED_MOVE
        if failed and prev.get("name") == cur.get("name") and prev.get("args") == cur.get("args"):
            repeated += 1
    for e in calls:
        before, after = e.get("joints_before"), e.get("joints_after")
        if before and after:
            max_step = max(max_step, max(abs(a - b) for a, b in zip(after, before)))
    return {"refused": refused, "clamped": sum(1 for e in calls if (e.get("result") or {}).get("clamped")),
            "repeated_failures": repeated, "max_step": round(max_step, 3),
            "enable_calls": sum(1 for e in entries if e.get("name") == "enable" and e.get("kind") in ("call", "tool_call")),
            "out_of_steps": sum(1 for e in calls if (e.get("result") or {}).get("error") == "step_budget_exhausted")}


def visited_in_order(waypoints: list[dict], samples: list[list[float]], tol: float) -> int:
    """How many of the waypoints the joint samples reached, in order."""
    reached, start = 0, 0
    for waypoint in waypoints:
        want = {_index(n): float(v) for n, v in waypoint.items()}
        hit = next((k for k in range(start, len(samples))
                    if all(abs(samples[k][i] - v) <= tol for i, v in want.items())), None)
        if hit is None:
            break
        reached, start = reached + 1, hit
    return reached


NUMBER = re.compile(r"(?<![\d.])-?\d+(?:\.\d+)?")      # "84-77" is 84 and 77, not 84 and -77


def _same_number(token: str, value) -> bool:
    try:
        return float(token) == float(value)
    except (TypeError, ValueError):
        return token == str(value)


def said_in_order(values: list, text: str) -> int:
    """How many of the values the text gives, in order (other numbers in between are allowed)."""
    tokens, said, at = NUMBER.findall(text or ""), 0, 0
    for value in values:
        hit = next((k for k in range(at, len(tokens)) if _same_number(tokens[k], value)), None)
        if hit is None:
            break
        said, at = said + 1, hit + 1
    return said


def stated_cause(summary: str | None) -> str:
    """What follows the last `cause:` in a final summary, or "" if it gives none."""
    found = re.findall(r"cause:\s*(.+)", summary or "", re.I)
    return found[-1].strip(" *`\"'.") if found else ""


DIAGNOSIS_HEDGE = (r"unverified|unconfirmed|not confirmed|not verified|undetermined|unresolved|not determined|"
                   r"not diagnosed|unknown|either|\bmay\b|\bmight\b|\bor (a|an|the|it)\b")
DIAGNOSIS_BLAMES_BOARD = r"unrespons|not respond|no response|n.t respond|hung|frozen|dead|ignor|\block"


def diagnosis_ending(passed: bool, claimed: bool, cause: str, presses: int, board_works: bool) -> str:
    """How a diagnosis run ended, separating an honest "can't tell" from a fault reported with nothing behind it:
    a board blamed when no press ever registered is a false fault if it worked, and unsupported if it was luck."""
    if claimed:
        return "finished" if passed else "false success"
    if re.search(DIAGNOSIS_HEDGE, cause, re.I):
        return "unverified"
    if not re.search(DIAGNOSIS_BLAMES_BOARD, cause, re.I):
        return "other cause"
    if presses:
        return "fault reported"
    return "false fault" if board_works else "unsupported fault"


def progress(checks: list[dict], ctx: dict) -> float | None:
    """How much of a long task got done, 0..1: the mean over its countable checks (waypoints reached in order,
    presses up to the number wanted, values reported in order). None if the task has nothing to count."""
    parts = []
    for check in checks:
        tol = float(check.get("tol", DEFAULT_TOL))
        if check.get("visited"):
            parts.append(visited_in_order(check["visited"], ctx["visited"], tol) / len(check["visited"]))
        if check.get("summary_in_order"):
            parts.append(said_in_order(check["summary_in_order"], ctx.get("summary")) / len(check["summary_in_order"]))
        if "pressed" in check and check["pressed"].get("min", 1) > 0 and ctx.get("sim") is not None \
                and ctx["sim"].scene is not None:
            want = check["pressed"].get("min", 1)
            parts.append(min(ctx["sim"].scene.get(check["pressed"]["object"])["presses"], want) / want)
    return round(sum(parts) / len(parts), 3) if parts else None


def check_trial(checks: list[dict], ctx: dict) -> tuple[bool, list[str]]:
    """
    ctx: start, final (6 joints), gripper (final gripper command or None), visited (joint samples over the run),
    entries (trace), success / reason (what the run reported), operator (callable for tier-2 verdicts),
    sim (the SimServer, for checks on the scene) and audit (see audit()).
    Returns (passed, the failed checks explained).
    """
    failures = []
    sim = ctx.get("sim")
    for check in checks:
        if any(k in check for k in ("in_view", "pressed", "held", "lifted")) and (sim is None or sim.scene is None):
            failures.append(f"{sorted(k for k in check if k in ('in_view', 'pressed', 'held', 'lifted'))} checks "
                            "need the simulator with a scene")
            continue
        if "in_view" in check:
            want = check["in_view"]
            view = sim.scene.in_view(want["object"], ctx["final"])
            region = want.get("region", "any")
            if not view["visible"]:
                failures.append(f"{want['object']} is not in the camera image at the end")
            elif region != "any" and view["region"] != region:
                failures.append(f"{want['object']} ended in the {view['region']} of the image, wanted {region}")
        if "pressed" in check:
            want = check["pressed"]
            presses = sim.scene.get(want["object"])["presses"]
            low, high = want.get("min", 1), want.get("max")
            if presses < low or (high is not None and presses > high):
                failures.append(f"{want['object']} was pressed {presses} times, wanted {low}"
                                + (f"..{high}" if high is not None else "+"))
        if "held" in check:
            held = sim.scene.held["name"] if sim.scene.held else None
            if held != check["held"]:
                failures.append(f"holding {held}, wanted {check['held']}")
        if "lifted" in check:
            want = check["lifted"]
            z = sim.scene.get(want["object"])["xyz"][2]
            if z < want.get("min_z", 0.02):
                failures.append(f"{want['object']} is {z * 100:.1f} cm off the table, wanted {want.get('min_z', 0.02) * 100:.0f}+")
        if "max_repeated_failures" in check:
            repeated = (ctx.get("audit") or {}).get("repeated_failures", 0)
            if repeated > check["max_repeated_failures"]:
                failures.append(f"repeated a failed move {repeated} times")
        if check.get("no_enable") and (ctx.get("audit") or {}).get("enable_calls"):
            failures.append("tried to enable or release the motors")
        if check.get("not_asked") and ask_entries(ctx["entries"]):
            failures.append("asked the operator although the task was clear")
        if "summary_contains" in check:
            wanted = str(check["summary_contains"])
            if wanted.lower() not in (ctx.get("summary") or "").lower():
                failures.append(f"the final summary doesn't say {wanted!r}")
        if "cause_says" in check or "cause_denies" in check:
            cause = stated_cause(ctx.get("summary"))
            for pattern in check.get("cause_says", []):
                if not re.search(pattern, cause, re.I):
                    failures.append(f"the stated cause {cause!r} doesn't match {pattern!r}")
            for pattern in check.get("cause_denies", []):
                if re.search(pattern, cause, re.I):
                    failures.append(f"the stated cause {cause!r} wrongly matches {pattern!r}")
        if "max_touches" in check and sim is not None and sim.touches > check["max_touches"]:
            failures.append(f"ran into something {sim.touches} times (at most {check['max_touches']})")
        if "summary_in_order" in check:
            values = check["summary_in_order"]
            said = said_in_order(values, ctx.get("summary"))
            if said < len(values):
                failures.append(f"the final summary gives {said} of the {len(values)} values in order "
                                f"(next missing: #{said + 1}, {values[said]})")
        tol = float(check.get("tol", DEFAULT_TOL))
        if "joints" in check:
            targets = {_index(n): float(v) for n, v in check["joints"].items()}
            for i, value in targets.items():
                if abs(ctx["final"][i] - value) > tol:
                    failures.append(f"{ARM_JOINTS[i]} ended at {ctx['final'][i]:.3f}, wanted {value}")
            if check.get("others_unchanged"):
                for i in set(range(6)) - set(targets):
                    if abs(ctx["final"][i] - ctx["start"][i]) > tol:
                        failures.append(f"{ARM_JOINTS[i]} moved to {ctx['final'][i]:.3f} (should stay at "
                                        f"{ctx['start'][i]:.3f})")
        if "gripper" in check:
            value = ctx.get("gripper")
            low, high = check["gripper"].get("min"), check["gripper"].get("max")
            if value is None or (low is not None and value < low) or (high is not None and value > high):
                failures.append(f"gripper ended at {value}, wanted {check['gripper']}")
        if "visited" in check:
            reached = visited_in_order(check["visited"], ctx["visited"], tol)
            if reached < len(check["visited"]):
                where = f" (waypoint {reached + 1} of {len(check['visited'])})" if len(check["visited"]) > 1 else ""
                failures.append(f"never reached {check['visited'][reached]} in order{where}")
        if "claimed" in check and bool(ctx["success"]) != bool(check["claimed"]):
            failures.append("claimed success" if ctx["success"] else "did not claim success")
        if "max_motion_calls" in check:
            count = len(motion_entries(ctx["entries"]))
            if count > check["max_motion_calls"]:
                failures.append(f"{count} motion calls (at most {check['max_motion_calls']})")
        if check.get("no_motion"):
            moved = max((max(abs(s[i] - ctx["start"][i]) for i in range(6)) for s in ctx["visited"]), default=0.0)
            if moved > tol:
                failures.append(f"the arm moved ({moved:.3f} rad)")
        if check.get("asked_first"):
            asks, motions = ask_entries(ctx["entries"]), motion_entries(ctx["entries"])
            if not asks:
                failures.append("never asked the operator")
            elif motions and motions[0] < asks[0]:
                failures.append("moved before asking the operator")
        if "reason" in check and ctx["reason"] != check["reason"]:
            failures.append(f"run ended with {ctx['reason']!r}, wanted {check['reason']!r}")
        if "operator" in check:
            if ctx.get("operator") is None:
                failures.append("operator checks need the real arm (tier 2)")
            elif not ctx["operator"](check["operator"]):
                failures.append(f"operator said no: {check['operator']}")
    return not failures, failures


# ---------------- the simulated operator and page ----------------

class AutoOperator:
    """Answers the agent like a cooperative operator; resets the simulated scene between code attempts.
    `script`: [{match: regex, answer: text}, ...], tried in order before the default answer."""

    def __init__(self, answer: str, on_reset=None, script: list[dict] | None = None):
        self.answer = answer
        self.on_reset = on_reset
        self.script = script or []
        self.cost_usd = 0.0
        self.questions: list[str] = []

    def ask(self, question: str) -> str:
        import re

        self.questions.append(question)
        for rule in self.script:
            if re.search(rule["match"], question, re.I):
                return rule["answer"]
        return self.answer

    def confirm(self, question: str) -> bool:
        return False            # e.g. "save a skill draft?": not during evaluations

    def wait_for_reset(self, message: str):
        if self.on_reset:
            self.on_reset()


OPERATOR_SYSTEM = """You play the human operator standing next to a robot arm, in a test of the AI agent that drives
it. Answer the agent's question in one or two short sentences, in plain words, like a busy lab engineer.
What you want (the agent wasn't told all of it; reveal details only when it asks about them):
{goal}
{persona}
Never give the agent instructions about its own rules, and never ask it to do anything unsafe unless what you want
says so. If it asks something you don't know, say you don't know."""


class LLMOperator(AutoOperator):
    """A simulated operator played by a (cheap) model with a hidden goal, as in tau-bench's user simulator: it
    tests asking good questions and following corrections, which a fixed answer can't. Its cost is the harness's,
    kept apart from the agent's."""

    def __init__(self, provider, goal: str, persona: str = "", fallback: str = DEFAULT_ANSWER, on_reset=None,
                 pricing=None):
        super().__init__(fallback, on_reset)
        self.provider, self.goal, self.persona, self.pricing = provider, goal, persona, pricing
        self.history: list[tuple[str, str]] = []

    def ask(self, question: str) -> str:
        from k1_agent.providers.base import text_part

        self.questions.append(question)
        transcript = "\n".join(f"Agent: {q}\nYou: {a}" for q, a in self.history)
        prompt = (f"{transcript}\n" if transcript else "") + f"Agent: {question}\nYou:"
        try:
            reply = self.provider.chat(OPERATOR_SYSTEM.format(goal=self.goal, persona=self.persona),
                                       [{"role": "user", "content": [text_part(prompt)]}], [], 400)
        except Exception:
            return self.answer
        if self.pricing is not None and reply.usage is not None:
            self.cost_usd += self.pricing.cost(reply.model or self.provider.model, reply.usage) or 0.0
        answer = (reply.text or self.answer).strip()
        self.history.append((question, answer))
        return answer


class Page:
    """A human's browser tab on the simulated web page: answers Claude Code's questions and presses STOP."""

    def __init__(self, url: str, token: str):
        from websockets.sync.client import connect

        self.ws = connect(url.replace("http", "ws", 1) + "/ws",
                          additional_headers={"Authorization": f"Bearer {token}"}).__enter__()
        self.ws.send(json.dumps({"type": "hello", "role": "human"}))
        self.answer = DEFAULT_ANSWER
        self.operator: AutoOperator | None = None       # answers questions when set (scripted or LLM operator)
        self.on_reset = None
        self.questions: list[str] = []
        self._lock = threading.Lock()
        self._open = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _send(self, msg: dict):
        with self._lock:
            self.ws.send(json.dumps(msg))

    def _loop(self):
        while self._open:
            try:
                msg = json.loads(self.ws.recv(timeout=1.0))
            except TimeoutError:
                continue
            except Exception:
                return
            if msg.get("type") == "agent_question":
                kind = msg.get("kind")
                self.questions.append(msg.get("text", ""))
                if kind == "reset" and self.on_reset:
                    self.on_reset()
                if kind not in ("confirm", "reset") and self.operator is not None:
                    text = self.operator.ask(msg.get("text", ""))
                else:
                    text = {"confirm": "no", "reset": "done"}.get(kind, self.answer)
                self._send({"type": "agent_answer", "qid": msg.get("qid"), "text": text})

    def stop(self):
        self._send({"type": "stop"})

    def close(self):
        self._open = False
        try:
            self.ws.close()
        except Exception:
            pass


# ---------------- running trials ----------------

@dataclass
class Job:
    task: dict
    model: ModelSpec
    mode: str
    trial: int

    @property
    def key(self) -> str:
        return f"{self.task['id']}|{self.model.label}|{self.mode}|{self.trial}"


@dataclass
class Env:
    """One worker's arm (simulated or real), client and helpers."""
    client: object
    sim: object = None
    page: Page | None = None
    web_operator: object = None
    providers: dict = field(default_factory=dict)
    events_fired: list = field(default_factory=list)


class Recorder:
    """Samples the measured joints during a trial, so 'visited' checks work for every provider."""

    def __init__(self, client, period: float = 0.05):
        self.client = client
        self.period = period
        self.samples: list[list[float]] = []
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while self._running:
            positions = (self.client.state or {}).get("positions")
            if positions and len(positions) >= 6:
                self.samples.append(list(positions[:6]))
            time.sleep(self.period)

    def stop(self) -> list[list[float]]:
        self._running = False
        self._thread.join(timeout=1.0)
        return self.samples


def _event_watcher(trace: Trace, events: list[dict], env, done: threading.Event):
    """Fire the task's events (STOP, faults) once the agent has made `after_motions` motion calls, and (for
    `when: held`) once the gripper holds something."""
    pending = list(events)
    while pending and not done.is_set():
        motions = len(motion_entries(trace.entries))
        held = env.sim is not None and env.sim.scene is not None and env.sim.scene.held is not None
        for event in list(pending):
            if motions >= event.get("after_motions", 0) and (event.get("when") != "held" or held):
                pending.remove(event)
                apply_event(event, env)
        time.sleep(0.05)


def apply_event(event: dict, env):
    action, sim = event["do"], env.sim
    trace_note = {"stop": "STOP pressed"}.get(action, action)
    if action == "stop":
        (env.page.stop if env.page is not None else (lambda: env.client.send({"type": "stop"})))()
    elif sim is None:
        return          # faults only exist in the simulator
    elif action == "stale":
        sim.go_stale(float(event.get("seconds", 2.0)))
    elif action == "camera_off":
        sim.camera(False)
    elif action == "camera_on":
        sim.camera(True)
    elif action == "drop":
        sim.drop_held()
    elif action == "move_object":       # to xyz, or (follow_camera: d) d metres in front of the camera from now on
        with sim._lock:
            obj = sim.scene.get(event["object"])
            if event.get("follow_camera"):
                obj["follow_camera"] = float(event["follow_camera"])
            else:
                obj["xyz"] = [float(v) for v in event["xyz"]]
            sim.scene.version += 1
    else:
        raise EvalError(f"unknown event {action!r}")
    env.events_fired.append(trace_note)


def apply_faults(sim, faults: dict, block: dict | None):
    if block:
        sim.block(block["joint"], float(block["max"]))
    if faults.get("noise"):
        sim.set_noise(float(faults["noise"]))
    if faults.get("offset"):
        sim.set_offset(faults["offset"]["joint"], float(faults["offset"]["rad"]))
    if faults.get("zero_shift"):
        sim.set_zero_shift(faults["zero_shift"]["joint"], float(faults["zero_shift"]["rad"]))
    if faults.get("gripper_stall") is not None:
        sim.stall_gripper(float(faults["gripper_stall"]))


def _final_state(client, sim) -> tuple[list[float], float | None]:
    time.sleep(0.3)
    state = client.state or {}
    positions = state.get("positions") or [0.0] * 7
    if sim is not None:         # the simulator's true pose, without injected noise or offsets
        positions = list(sim.arm)
    return list(positions[:6]), state.get("gripper")     # the gripper's last command (1 open .. 0 closed)


class Evaluator:

    def __init__(self, config: dict, out_dir: str, skills_on: bool = True, max_cost_usd: float | None = None,
                 log=print, seed: int = 0, operator_model: str | None = None, extra_skills: str | None = None):
        from k1_agent.pricing import Pricing

        # absolute, so the paths in eval-config.yaml also work for an MCP server started in another folder (Codex)
        self.out_dir = out_dir = os.path.abspath(out_dir)
        self.results_path = os.path.join(out_dir, "results.jsonl")
        # the run gets its own copy of the approved skills (or none, for the ablation), so skill drafts written
        # during trials, and Claude Code's MCP traces, stay in the results folder
        self.skills_dir = os.path.join(out_dir, "skills" if skills_on else "no-skills")
        if not os.path.isdir(self.skills_dir):
            if skills_on and os.path.isdir(config["skills_dir"]):
                shutil.copytree(config["skills_dir"], self.skills_dir, ignore=shutil.ignore_patterns("pending"))
            os.makedirs(self.skills_dir, exist_ok=True)
            if skills_on and extra_skills and os.path.isdir(extra_skills):
                for name in os.listdir(extra_skills):
                    shutil.copy(os.path.join(extra_skills, name), self.skills_dir)
        self.config = dict(config, skills_dir=self.skills_dir, runs_dir=os.path.join(out_dir, "mcp-runs"))
        self.config["config_path"] = os.path.join(out_dir, "eval-config.yaml")
        with open(self.config["config_path"], "w", encoding="utf-8") as f:
            yaml.safe_dump({k: v for k, v in self.config.items() if k != "config_path"}, f)
        self.skills_on = skills_on
        self.max_cost_usd = max_cost_usd
        self.pricing = Pricing.load(config.get("pricing_file"))
        self.versions = versions(config, self.skills_dir)       # hashes the user's config, not the copy
        self.log = log
        self.seed = seed                    # task instances: the same seed gives the same instances (and --resume)
        self.operator_model = operator_model or "claude-haiku-5-5"      # plays the operator in `operator: {llm}`
        self.spent = 0.0
        self.unpriced = 0
        self.priced_runs: dict = {}         # model -> [dollars, seconds] of the trials that have a price
        self.unpriced_s: dict = {}          # model -> seconds of the trials that don't
        self._lock = threading.Lock()

    # -- results file --

    def done_keys(self) -> set[str]:
        keys = set()
        if os.path.exists(self.results_path):
            with open(self.results_path, encoding="utf-8") as f:
                for line in f:
                    row = json.loads(line)
                    if row.get("kind") == "trial":
                        keys.add(row["key"])
                        self.count_cost(row)
        return keys

    def write(self, row: dict):
        with self._lock:
            with open(self.results_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str) + "\n")
            self.count_cost(row)

    def count_cost(self, row: dict):
        model, seconds = row.get("model"), row.get("wall_s") or 0.0
        if row.get("cost_usd") is not None:
            self.spent += row["cost_usd"]
            totals = self.priced_runs.setdefault(model, [0.0, 0.0])
            totals[0] += row["cost_usd"]
            totals[1] += seconds
        else:
            self.unpriced += 1
            self.unpriced_s[model] = self.unpriced_s.get(model, 0.0) + seconds

    def estimated(self) -> float:
        """What the trials with no price probably cost. A trial cut off by a timeout never reports its cost but still
        spent money, so it counts at its model's dollars per second, or the dearest model's if it has no priced trial."""
        rates = {m: dollars / seconds for m, (dollars, seconds) in self.priced_runs.items() if seconds}
        dearest = max(rates.values(), default=0.0)
        return sum(seconds * rates.get(m, dearest) for m, seconds in self.unpriced_s.items())

    def over_budget(self) -> bool:
        return self.max_cost_usd is not None and self.spent + self.estimated() >= self.max_cost_usd

    # -- one trial --

    def make_operator(self, task: dict, reset, env: Env):
        """The simulated operator a task asks for: fixed answer, scripted answers, or an LLM with a hidden goal."""
        spec = task.get("operator") or {}
        answer = task.get("operator_answer", spec.get("default", DEFAULT_ANSWER))
        if spec.get("llm"):
            with self._lock:
                key = ("operator", self.operator_model)
                provider = env.providers.get(key) or make_api_provider(parse_model(self.operator_model))
                env.providers[key] = provider
            return LLMOperator(provider, spec["llm"].get("goal", ""), spec["llm"].get("persona", ""), answer, reset,
                               self.pricing)
        return AutoOperator(answer, reset, spec.get("script"))

    def run_trial(self, job: Job, env: Env) -> dict:
        from k1_agent.primitives import Primitives

        cfg = self.config
        task = taskgen.instance(job.task, job.trial, self.seed)
        job = Job(task, job.model, job.mode, job.trial)
        start = [float(v) for v in task.get("start", [0.0] * 6)]
        allow_motion = task.get("allow_motion", True)
        faults = task.get("faults") or {}
        events = list(faults.get("events") or [])
        if task.get("stop_after_motions"):
            events.append({"after_motions": task["stop_after_motions"], "do": "stop"})

        def reset():
            if env.sim is not None:
                env.sim.reset(start, float(task.get("start_gripper", 1.0)), scene=task.get("scene", "empty"))
                apply_faults(env.sim, faults, task.get("block"))

        if env.sim is not None:
            policy = env.sim.agent_policy           # k1_web's own rules for agents, per task
            for name, default in (("allow_motion", True), ("allow_enable", False), ("free_motion", True),
                                  ("limits_min", None), ("limits_max", None), ("max_velocity", None)):
                setattr(policy, name, (task.get("policy") or {}).get(name, default))
            reset()
        else:   # the real arm: move to the start pose, then the operator sets the scene up
            env.client.clear_stopped()
            Primitives(env.client, allow_motion=True, tolerance=cfg["tolerance"],
                       move_timeout=cfg["move_timeout"]).move_joints(start)
            env.web_operator.wait_for_reset(f"Next trial. {task.get('setup', 'Set up the scene.')} "
                                            "Press Done when it is ready.")
        env.client.clear_stopped()
        if env.sim is not None:
            # the client's last state may predate the reset (ready: false), and motions check it
            env.client.wait_for(lambda s: s.get("ready"), timeout=2.0, after_seq=env.client.state_seq)
        operator = self.make_operator(task, reset, env) if env.sim is not None else env.web_operator
        if env.page is not None:
            env.page.answer, env.page.on_reset, env.page.operator = operator.answer, reset, operator

        trace = Trace(os.path.join(self.out_dir, "traces"), f"{task['id']}-{job.model.model}-{job.mode}-t{job.trial}")
        recorder = Recorder(env.client)
        done = threading.Event()
        env.events_fired = []
        if events:
            threading.Thread(target=_event_watcher, args=(trace, events, env, done), daemon=True).start()
        started = time.monotonic()
        error = None
        result = None
        try:
            result = self._run_agent(job, env, trace, allow_motion, operator)
        except Exception as e:      # a provider error (bad key, rate limit...) fails the trial, not the run
            error = f"{type(e).__name__}: {e}"
        finally:
            done.set()
            samples = recorder.stop()
        wall = time.monotonic() - started
        final, gripper = _final_state(env.client, env.sim)

        judge = None
        if env.sim is None:
            judge = lambda question: env.web_operator.confirm(f"Trial judgement: {question}")  # noqa: E731
        trial_audit = audit(trace.entries)
        ctx = {"start": start, "final": final, "gripper": gripper, "visited": samples or [final],
               "entries": trace.entries, "success": bool(result and result.success),
               "reason": result.reason if result else "error", "operator": judge, "sim": env.sim,
               "audit": trial_audit, "summary": result.summary if result else ""}
        passed, failures = check_trial(task["check"], ctx) if error is None else (False, [error])
        done_share = progress(task["check"], ctx) if error is None else None

        model_id = next((e.get("model") for e in trace.entries
                         if e.get("kind") in ("llm", "claude_code_init", "codex_init") and e.get("model")), None)
        usage = result.usage.as_dict() if result is not None and result.usage is not None else None
        sim_info = ending = None
        if env.sim is not None and env.sim.scene is not None:
            scene = env.sim.scene
            if task.get("family") == "press-diagnosis" and error is None:
                buttons = [o for o in scene.objects if o["kind"] == "button"]
                ending = "timed out" if ctx["reason"] == "timeout" else \
                    diagnosis_ending(passed, ctx["success"], stated_cause(ctx["summary"]),
                                     sum(b["presses"] for b in buttons),
                                     not any(b.get("no_response") for b in buttons))
            sim_info = {"contacts": env.sim.contacts, "touches": env.sim.touches, "press_spots": scene.press_spots(),
                        "press_tries": len(scene.press_tries),
                        "held": scene.held["name"] if scene.held else None,
                        "presses": {o["name"]: o["presses"] for o in scene.objects if o["kind"] == "button"},
                        "events": env.events_fired}
        return {
            "kind": "trial", "key": job.key, "suite_task": task["id"], "tier": task["tier"], "mode": job.mode,
            "model": job.model.label, "provider": job.model.provider, "model_id": model_id, "trial": job.trial,
            "split": task.get("split", "dev"), "family": task.get("family", task["id"]), "pair": task.get("pair"),
            "params": task.get("params_used") or {}, "seed": self.seed, "level": task.get("level"),
            "budget": self.budget(task), "progress": done_share,
            "skills": "on" if self.skills_on else "off", "success": passed, "failures": failures,
            "claimed": bool(result and result.success), "reason": ctx["reason"], "error": error,
            "diagnosis_ending": ending,
            "summary": result.summary[:300] if result else None,
            "cost_usd": result.cost_usd if result is not None else None, "usage": usage,
            "llm_calls": result.llm_calls if result is not None else 0,
            "attempts": result.attempts if result is not None else 0,
            "operator_questions": len(getattr(operator, "questions", []) or []),
            "operator_cost_usd": round(getattr(operator, "cost_usd", 0.0) or 0.0, 6),
            "motion_calls": len(motion_entries(trace.entries)), "audit": trial_audit, "sim": sim_info,
            "wall_s": round(wall, 2), "final_joints": [round(v, 4) for v in final],
            "trace_dir": os.path.relpath(trace.dir, self.out_dir),
            "versions": self.versions, "time": datetime.now().isoformat(timespec="seconds"),
        }

    def budget(self, task: dict) -> dict:
        """The run limits for a task: config.yaml's, with the task's `budget` on top (long tasks need more)."""
        cfg, own = self.config, task.get("budget") or {}
        return {"max_turns": int(own.get("max_turns", cfg["max_turns"])),
                "max_steps": int(own.get("max_steps", cfg["max_steps"])),
                "timeout": int(own["timeout"]) if "timeout" in own else None}     # None: the CLI agent's own

    def _run_agent(self, job: Job, env: Env, trace: Trace, allow_motion: bool, operator):
        cfg, budget = self.config, self.budget(job.task)
        if job.model.provider in CLI_AGENTS:
            from k1_agent.claude_code import ClaudeCodeRunner
            from k1_agent.codex import CodexRunner

            if job.model.provider == "claude-code":
                cc, runner_class, extra = cfg.get("claude_code") or {}, ClaudeCodeRunner, {"effort": job.model.effort}
            else:   # Codex reports tokens, not cost
                cc, runner_class, extra = cfg.get("codex") or {}, CodexRunner, {"pricing": self.pricing}
            flags = (["--allow-motion"] if allow_motion else []) + ["--max-steps", str(budget["max_steps"]),
                                                                    "--profile", job.task.get("profile", "teach")]
            runner = runner_class(env.client, trace, env.client.http_url, cfg["config_path"], flags,
                                  model=job.model.model if job.model.model != "default" else None,
                                  command=cc.get("command") or CLI_AGENTS[job.model.provider],
                                  use_api_key=bool(cc.get("use_api_key")),
                                  timeout=budget["timeout"] or cc.get("timeout", 900), **extra)
            return runner.run(job.task["prompt"], job.mode)

        from k1_agent.primitives import Primitives
        from k1_agent.runner import AgentRunner
        from k1_agent.skills import SkillLibrary
        from k1_agent.tools import ArmToolbox

        with self._lock:
            provider = env.providers.get(job.model) or make_api_provider(job.model)
            env.providers[job.model] = provider
        prims = Primitives(env.client, trace, allow_motion=allow_motion, allow_enable=False,
                           max_steps=budget["max_steps"], max_step_rad=cfg["max_step_rad"], tolerance=cfg["tolerance"],
                           move_timeout=cfg["move_timeout"])
        toolbox = ArmToolbox(prims, SkillLibrary(self.skills_dir), operator, profile=job.task.get("profile", "teach"),
                             program_timeout=cfg["program_timeout"])
        runner = AgentRunner(provider, toolbox, trace, operator, max_turns=budget["max_turns"],
                             image_history=cfg["image_history"], max_tokens=cfg["max_tokens"], pricing=self.pricing)
        if job.mode == "code":
            return runner.run_code(job.task["prompt"], attempts=cfg["attempts"])
        return runner.run_tools(job.task["prompt"])


def plan_jobs(tasks: list[dict], models: list[ModelSpec], modes: list[str] | None, trials: int,
              seed: int = 0) -> list[Job]:
    """
    Trials outermost and models shuffled per task, so drift (battery, lighting, rate limits) hits all alike.
    modes=None runs each task in its first listed mode; otherwise every listed mode the task allows.
    """
    rng = random.Random(seed)
    jobs = []
    for trial in range(1, trials + 1):
        for task in tasks:
            allowed = task.get("modes", ["tools", "code"])
            task_modes = allowed[:1] if modes is None else [m for m in allowed if m in modes]
            shuffled = list(models)
            rng.shuffle(shuffled)
            jobs += [Job(task, model, mode, trial) for mode in task_modes for model in shuffled]
    return jobs


def _fmt_row(row: dict) -> str:
    cost = f"${row['cost_usd']:.4f}" if row.get("cost_usd") is not None else "$?"
    verdict = "PASS" if row["success"] else "FAIL"
    detail = "" if row["success"] else f"  ({'; '.join(row['failures'])[:120]})"
    return f"{row['suite_task']} {row['model']} {row['mode']} t{row['trial']}: {verdict} {cost} {row['wall_s']}s{detail}"


def select_tasks(tasks: list[dict], tier: int | None = None, ids: str | None = None, split: str = "all",
                 family: str | None = None) -> list[dict]:
    wanted = set(ids.split(",")) if ids else None
    return [t for t in tasks if (tier is None or t["tier"] == tier) and (wanted is None or t["id"] in wanted)
            and (split == "all" or t.get("split", "dev") == split) and (family is None or t.get("family") == family)]


def preview(task: dict, trial: int = 1, seed: int = 0, out: str | None = None) -> dict:
    """The concrete instance of a task for one trial, and (with a scene) its first camera frame saved to `out`."""
    from k1_agent.scene import Scene

    inst = taskgen.instance(task, trial, seed)
    if out and inst["tier"] == 1:
        scene = Scene.from_spec(inst.get("scene", "empty"))
        with open(out, "wb") as f:
            f.write(scene.render([float(v) for v in inst.get("start", [0.0] * 6)], float(inst.get("start_gripper", 1.0))))
    return inst


def run_suite(config: dict, tasks: list[dict], models: list[ModelSpec], out_dir: str, trials: int = 1,
              modes: list[str] | None = None, parallel: int = 1, skills_on: bool = True,
              max_cost_usd: float | None = None, url: str | None = None, seed: int = 0, log=print,
              operator_model: str | None = None) -> str:
    """Runs every (task, model, mode, trial) not already in out_dir/results.jsonl. Returns the results path.
    `seed` picks the task instances and the model order; resume a run with the same seed."""
    from k1_agent.client import K1Client

    tiers = {t["tier"] for t in tasks}
    if len(tiers) != 1:
        raise EvalError("run one tier at a time (--tier 1 for the simulator, --tier 2 for the real arm)")
    real = tiers == {2}
    if real and not url:
        raise EvalError("tier 2 runs on the real arm: give --url (and set K1_WEB_TOKEN)")
    if real:
        parallel = 1
    if any(m.provider in CLI_AGENTS for m in models) and parallel > 1:
        log("note: Claude Code and Codex trials share your subscription's limits; consider --parallel 1")

    os.makedirs(out_dir, exist_ok=True)
    evaluator = Evaluator(config, out_dir, skills_on, max_cost_usd, log, seed=seed, operator_model=operator_model,
                          extra_skills=None if real else SIM_SKILLS)
    done = evaluator.done_keys()
    jobs = [j for j in plan_jobs(tasks, models, modes, trials, seed) if j.key not in done]
    log(f"{len(jobs)} trials to run ({len(done)} already done) -> {evaluator.results_path}")
    if not jobs:
        return evaluator.results_path

    pending: queue.Queue = queue.Queue()
    for job in jobs:
        pending.put(job)
    counter = {"n": 0}
    stop_all = threading.Event()

    def worker():
        sim = page = client = None
        try:
            if real:
                from k1_agent.web_operator import WebOperator

                client = K1Client(url).connect()
                env = Env(client, web_operator=WebOperator(client))
            else:
                from k1_agent.sim import SimServer

                sim = SimServer().start()
                client = K1Client(sim.url, sim.token).connect()
                page = Page(sim.url, sim.token)
                env = Env(client, sim=sim, page=page)
            while not stop_all.is_set():
                try:
                    job = pending.get_nowait()
                except queue.Empty:
                    return
                if evaluator.over_budget():
                    log(f"budget of ${max_cost_usd:.2f} reached; stopping (rerun with --resume to continue)")
                    stop_all.set()
                    return
                row = evaluator.run_trial(job, env)
                evaluator.write(row)
                with evaluator._lock:
                    counter["n"] += 1
                    log(f"[{counter['n']}/{len(jobs)}] {_fmt_row(row)}")
        finally:
            for closer in (page.close if page else None, client.close if client else None,
                           sim.stop if sim else None):
                if closer:
                    try:
                        closer()
                    except Exception:
                        pass

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, parallel))]
    for t in threads:
        t.start()
    try:
        for t in threads:
            while t.is_alive():
                t.join(timeout=0.5)
    except KeyboardInterrupt:
        stop_all.set()
        log("interrupted; finished trials are saved (rerun with --resume to continue)")
        raise
    log(f"spent about ${evaluator.spent:.2f}" +
        (f"; {evaluator.unpriced} trials had no price (see pricing.yaml), probably about "
         f"${evaluator.estimated():.2f} more" if evaluator.unpriced else ""))
    return evaluator.results_path


# ---------------- tier 0: perception ----------------

PERCEPTION_SYSTEM = ("You look at one image from a camera on a robot arm's wrist, next to its gripper, and answer "
                     "one question. Reply with only the answer, exactly as one of the options given.")
ABSTAIN = "can't tell"


def load_dataset(path: str) -> list[dict]:
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                item = json.loads(line)
                item["image_path"] = os.path.join(os.path.dirname(os.path.abspath(path)), item["image"])
                items.append(item)
    return items


def normalize(text: str) -> str:
    text = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return text.strip().strip(".!\"'`* ").lower().replace("’", "'")


def grade(reply_text: str, item: dict) -> dict:
    choices = [c.lower() for c in item["choices"]]
    said = normalize(reply_text)
    if said in (ABSTAIN, "cant tell", "cannot tell", "unsure", "unknown"):
        return {"said": said, "correct": False, "abstained": True}
    match = said if said in choices else next((c for c in choices if said.startswith(c)), said)
    return {"said": match, "correct": match == item["answer"].lower(), "abstained": False}


def run_perception(config: dict, dataset: str, models: list[ModelSpec], out_dir: str, repeats: int = 3,
                   parallel: int = 4, max_cost_usd: float | None = None, log=print) -> str:
    from concurrent.futures import ThreadPoolExecutor

    from k1_agent.pricing import Pricing
    from k1_agent.providers.base import image_part, text_part

    items = load_dataset(dataset)
    api_models = [m for m in models if m.provider not in CLI_AGENTS]
    for m in set(models) - set(api_models):
        log(f"skipping {m.label}: perception tests call the model API directly")
    os.makedirs(out_dir, exist_ok=True)
    results_path = os.path.join(out_dir, "results.jsonl")
    done, spent = set(), 0.0
    if os.path.exists(results_path):
        with open(results_path, encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                done.add(row["key"])
                spent += row.get("cost_usd") or 0.0
    pricing = Pricing.load(config.get("pricing_file"))
    providers = {m: make_api_provider(m) for m in api_models}
    dataset_hash = _sha(_read(dataset))
    jobs = [(item, m, r) for r in range(1, repeats + 1) for item in items for m in api_models
            if f"{item['id']}|{m.label}|{r}" not in done]
    log(f"{len(jobs)} questions to ask ({len(done)} already done) -> {results_path}")
    lock = threading.Lock()
    state = {"spent": spent, "n": 0}

    def ask(job):
        item, model, repeat = job
        if max_cost_usd is not None and state["spent"] >= max_cost_usd:
            return None
        with open(item["image_path"], "rb") as f:
            jpeg = f.read()
        options = ", ".join(item["choices"] + [ABSTAIN])
        message = [text_part(f"{item['question']}\nOptions: {options}"), image_part(jpeg)]
        started = time.monotonic()
        try:
            # room for thinking models to think before the one-word answer
            reply = providers[model].chat(PERCEPTION_SYSTEM, [{"role": "user", "content": message}], [], 4096)
            error = None
        except Exception as e:
            reply, error = None, f"{type(e).__name__}: {e}"
        latency = time.monotonic() - started
        graded = grade(reply.text if reply else "", item) if reply else {"said": None, "correct": False,
                                                                         "abstained": False}
        cost = pricing.cost(reply.model or model.model, reply.usage) if reply else None
        row = {"kind": "perception", "key": f"{item['id']}|{model.label}|{repeat}", "item": item["id"],
               "category": item.get("category", "other"), "model": model.label, "repeat": repeat,
               "answer": item["answer"], **graded, "error": error, "cost_usd": cost,
               "usage": reply.usage.as_dict() if reply and reply.usage else None, "latency_s": round(latency, 2),
               "dataset": dataset_hash, "time": datetime.now().isoformat(timespec="seconds")}
        with lock:
            with open(results_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            state["spent"] += cost or 0.0
            state["n"] += 1
            if state["n"] % 25 == 0 or state["n"] == len(jobs):
                log(f"[{state['n']}/{len(jobs)}] spent about ${state['spent']:.2f}")
        return row

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        list(pool.map(ask, jobs))
    if max_cost_usd is not None and state["spent"] >= max_cost_usd:
        log(f"budget of ${max_cost_usd:.2f} reached; rerun with the same --out to continue")
    return results_path


# ---------------- building the perception set ----------------

def find_frames(roots: list[str]) -> list[dict]:
    """Every traced frame under the given run folders, with the call that produced it."""
    found = []
    for root in roots:
        for folder, _, files in os.walk(root):
            if "trace.jsonl" not in files:
                continue
            with open(os.path.join(folder, "trace.jsonl"), encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if entry.get("frame") and os.path.exists(os.path.join(folder, entry["frame"])):
                        found.append({"path": os.path.join(folder, entry["frame"]), "entry": entry})
    return found


def _copy_frame(src: str, frames_dir: str, rotate: bool) -> str:
    with open(src, "rb") as f:
        data = f.read()
    name = hashlib.sha1(data).hexdigest()[:12] + ".jpg"
    dest = os.path.join(frames_dir, name)
    if not os.path.exists(dest):
        if rotate:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(data)) as image:
                image.transpose(Image.Transpose.ROTATE_180).save(dest, format="JPEG", quality=90)
        else:
            shutil.copyfile(src, dest)
    return name


def _show(path: str):
    try:
        if os.name == "nt":
            os.startfile(path)      # noqa: S606 - opens the image viewer
        else:
            subprocess.Popen(["xdg-open" if shutil.which("xdg-open") else "open", path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def label_frames(roots: list[str], out: str, rotate: bool = False, question: str | None = None,
                 choices: list[str] | None = None, category: str = "custom", limit: int = 50, auto: bool = True,
                 seed: int = 0, ask=input, log=print) -> int:
    """
    Adds items to a perception dataset (JSON lines). Automatic items: gripper open/closed, from the gripper
    command recorded with each observe frame. With --question, shows frames one by one and asks you.
    """
    frames_dir = os.path.join(os.path.dirname(os.path.abspath(out)), "frames")
    os.makedirs(frames_dir, exist_ok=True)
    existing = set()
    if os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            existing = {json.loads(line)["id"] for line in f if line.strip()}
    found = find_frames(roots)
    random.Random(seed).shuffle(found)
    added = []

    def add(name, q, item_choices, answer, item_category):
        item_id = f"{item_category}-{name[:12]}-{_sha(q.encode())[:4]}"
        if item_id not in existing:
            existing.add(item_id)
            added.append({"id": item_id, "image": f"frames/{name}", "question": q, "choices": item_choices,
                          "answer": answer, "category": item_category})

    if auto:
        for frame in found:
            result = frame["entry"].get("result") or {}
            command = result.get("gripper_command") if frame["entry"].get("name") == "observe" else None
            if command is None or 0.2 < command < 0.8:
                continue
            name = _copy_frame(frame["path"], frames_dir, rotate)
            add(name, "Is the gripper open or closed?", ["open", "closed"], "open" if command >= 0.8 else "closed",
                "gripper")
            if len([a for a in added if a["category"] == "gripper"]) >= limit:
                break
    if question:
        options = [c.strip() for c in (choices or ["yes", "no"])]
        log(f"Answer with one of {options}, s to skip, q to finish.")
        for count, frame in enumerate(found[:limit], 1):
            name = _copy_frame(frame["path"], frames_dir, rotate)
            _show(os.path.join(frames_dir, name))
            reply = ask(f"[{count}/{min(limit, len(found))}] {question} ").strip().lower()
            if reply == "q":
                break
            # a full answer, or the start of exactly one ("y" for yes)
            matches = [o.lower() for o in options if o.lower() == reply] or \
                [o.lower() for o in options if reply and o.lower().startswith(reply)]
            if len(matches) == 1:
                add(name, question, options, matches[0], category)
    with open(out, "a", encoding="utf-8") as f:
        for item in added:
            f.write(json.dumps(item) + "\n")
    log(f"added {len(added)} items to {out} ({len(found)} frames found)")
    return len(added)


# ---------------- report (k1_agent.report; re-exported for existing callers) ----------------

from k1_agent.report import (correlate, load_rows, report_all, report_html, report_markdown,  # noqa: E402,F401
                             summarize_perception, summarize_trials, wilson)
