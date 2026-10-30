"""
Model evaluation, ASPIRE-style: success rate (decided by a checker, never by the model's own claim) against
cost, for several models on the same tasks.

    k1-agent eval run evals/tasks.yaml --tier 1 --models claude-haiku-5-5,gpt-5.5 --trials 10
    k1-agent eval perception evals/perception.jsonl --models claude-opus-5-5,gemini-4-argon
    k1-agent eval label path/to/runs --out evals/perception.jsonl --question "Is a red block in view?" --choices yes,no
    k1-agent eval report evals/results/<run>

Tier 1 runs in the simulated arm (k1_agent.sim), on any computer. Tier 2 runs on the real arm (--url), with an
operator who resets the scene and judges trials on the web page without being told the model.
Every trial writes one row to <out>/results.jsonl and keeps its trace in <out>/traces/.
"""
import hashlib
import json
import math
import os
import queue
import random
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

import yaml

from k1_agent.trace import Trace

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARM_JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_roll", "wrist_flex", "gripper_roll"]
MOTION_NAMES = {"move_joints", "move_joint_delta", "go_pose", "go_home", "set_gripper"}
DEFAULT_ANSWER = "Yes, go ahead."
DEFAULT_TOL = 0.03
CLI_AGENTS = {"claude-code": "claude", "codex": "codex"}     # providers run as a coding agent's CLI


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
    with open(path, encoding="utf-8") as f:
        tasks = (yaml.safe_load(f) or {}).get("tasks") or []
    ids = [t.get("id") for t in tasks]
    if len(set(ids)) != len(ids) or not all(ids):
        raise EvalError(f"{path}: every task needs a unique id")
    for task in tasks:
        if not task.get("prompt") or not task.get("check"):
            raise EvalError(f"{path}: task {task['id']} needs a prompt and a check")
        if task.get("tier") not in (1, 2):
            raise EvalError(f"{path}: task {task['id']} needs tier: 1 (simulated) or 2 (real arm)")
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
    from k1_agent import claude_code, runner

    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=AGENT_DIR, capture_output=True,
                                text=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "."], cwd=AGENT_DIR, capture_output=True,
                               text=True, timeout=10).stdout.strip()
        commit = commit + ("+dirty" if dirty else "") if commit else "unknown"
    except (OSError, subprocess.SubprocessError):
        commit = "unknown"
    prompts = (runner.SYSTEM_PROMPT + runner.CODE_INSTRUCTIONS + claude_code.FINISH_RULES).encode()
    return {"commit": commit, "config": _sha(_read(config["config_path"])), "skills": skills_hash(skills_dir),
            "prompts": _sha(prompts, _read(os.path.join(AGENT_DIR, "AGENTS.md")))}


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


def check_trial(checks: list[dict], ctx: dict) -> tuple[bool, list[str]]:
    """
    ctx: start, final (6 joints), gripper (final gripper value or None), visited (joint samples over the run),
    entries (trace), success / reason (what the run reported), operator (callable for tier-2 verdicts).
    Returns (passed, the failed checks explained).
    """
    failures = []
    for check in checks:
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
            samples, start = ctx["visited"], 0
            for waypoint in check["visited"]:
                want = {_index(n): float(v) for n, v in waypoint.items()}
                hit = next((k for k in range(start, len(samples))
                            if all(abs(samples[k][i] - v) <= tol for i, v in want.items())), None)
                if hit is None:
                    failures.append(f"never reached {waypoint} (in order)")
                    break
                start = hit
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
    """Answers the agent like a cooperative operator; resets the simulated scene between code attempts."""

    def __init__(self, answer: str, on_reset=None):
        self.answer = answer
        self.on_reset = on_reset

    def ask(self, question: str) -> str:
        return self.answer

    def confirm(self, question: str) -> bool:
        return False            # e.g. "save a skill draft?": not during evaluations

    def wait_for_reset(self, message: str):
        if self.on_reset:
            self.on_reset()


class Page:
    """A human's browser tab on the simulated web page: answers Claude Code's questions and presses STOP."""

    def __init__(self, url: str, token: str):
        from websockets.sync.client import connect

        self.ws = connect(url.replace("http", "ws", 1) + "/ws",
                          additional_headers={"Authorization": f"Bearer {token}"}).__enter__()
        self.ws.send(json.dumps({"type": "hello", "role": "human"}))
        self.answer = DEFAULT_ANSWER
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


def _stop_watcher(trace: Trace, motions: int, press, done: threading.Event):
    while not done.is_set():
        if len(motion_entries(trace.entries)) >= motions:
            press()
            return
        time.sleep(0.05)


def _final_state(client, sim) -> tuple[list[float], float | None]:
    time.sleep(0.3)
    state = client.state or {}
    positions = state.get("positions") or [0.0] * 7
    # the simulator reports the gripper command as its measured value; on the real arm use the last command
    gripper = (positions[6] if len(positions) > 6 else None) if sim is not None else state.get("gripper")
    return list(positions[:6]), gripper


class Evaluator:

    def __init__(self, config: dict, out_dir: str, skills_on: bool = True, max_cost_usd: float | None = None,
                 log=print):
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
        self.config = dict(config, skills_dir=self.skills_dir, runs_dir=os.path.join(out_dir, "mcp-runs"))
        self.config["config_path"] = os.path.join(out_dir, "eval-config.yaml")
        with open(self.config["config_path"], "w", encoding="utf-8") as f:
            yaml.safe_dump({k: v for k, v in self.config.items() if k != "config_path"}, f)
        self.skills_on = skills_on
        self.max_cost_usd = max_cost_usd
        self.pricing = Pricing.load(config.get("pricing_file"))
        self.versions = versions(config, self.skills_dir)       # hashes the user's config, not the copy
        self.log = log
        self.spent = 0.0
        self.unpriced = 0
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
                        self.spent += row.get("cost_usd") or 0.0
        return keys

    def write(self, row: dict):
        with self._lock:
            with open(self.results_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str) + "\n")
            if row.get("cost_usd") is not None:
                self.spent += row["cost_usd"]
            else:
                self.unpriced += 1

    def over_budget(self) -> bool:
        return self.max_cost_usd is not None and self.spent >= self.max_cost_usd

    # -- one trial --

    def run_trial(self, job: Job, env: Env) -> dict:
        from k1_agent.primitives import Primitives

        task, cfg = job.task, self.config
        start = [float(v) for v in task.get("start", [0.0] * 6)]
        allow_motion = task.get("allow_motion", True)
        answer = task.get("operator_answer", DEFAULT_ANSWER)

        def reset():
            if env.sim is not None:
                env.sim.reset(start, float(task.get("start_gripper", 1.0)))
                if task.get("block"):
                    env.sim.block(task["block"]["joint"], float(task["block"]["max"]))

        if env.sim is not None:
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
        if env.page is not None:
            env.page.answer, env.page.on_reset = answer, reset

        trace = Trace(os.path.join(self.out_dir, "traces"), f"{task['id']}-{job.model.model}-{job.mode}-t{job.trial}")
        recorder = Recorder(env.client)
        done = threading.Event()
        if task.get("stop_after_motions"):
            press = env.page.stop if env.page is not None else (lambda: env.client.send({"type": "stop"}))
            threading.Thread(target=_stop_watcher, args=(trace, task["stop_after_motions"], press, done),
                             daemon=True).start()
        started = time.monotonic()
        error = None
        result = None
        try:
            result = self._run_agent(job, env, trace, allow_motion, answer, reset)
        except Exception as e:      # a provider error (bad key, rate limit...) fails the trial, not the run
            error = f"{type(e).__name__}: {e}"
        finally:
            done.set()
            samples = recorder.stop()
        wall = time.monotonic() - started
        final, gripper = _final_state(env.client, env.sim)

        operator = None
        if env.sim is None:
            operator = lambda question: env.web_operator.confirm(f"Trial judgement: {question}")  # noqa: E731
        ctx = {"start": start, "final": final, "gripper": gripper, "visited": samples or [final],
               "entries": trace.entries, "success": bool(result and result.success),
               "reason": result.reason if result else "error", "operator": operator}
        passed, failures = check_trial(task["check"], ctx) if error is None else (False, [error])

        model_id = next((e.get("model") for e in trace.entries
                         if e.get("kind") in ("llm", "claude_code_init", "codex_init") and e.get("model")), None)
        usage = result.usage.as_dict() if result is not None and result.usage is not None else None
        return {
            "kind": "trial", "key": job.key, "suite_task": task["id"], "tier": task["tier"], "mode": job.mode,
            "model": job.model.label, "provider": job.model.provider, "model_id": model_id, "trial": job.trial,
            "skills": "on" if self.skills_on else "off", "success": passed, "failures": failures,
            "claimed": bool(result and result.success), "reason": ctx["reason"], "error": error,
            "summary": result.summary[:300] if result else None,
            "cost_usd": result.cost_usd if result is not None else None, "usage": usage,
            "llm_calls": result.llm_calls if result is not None else 0,
            "motion_calls": len(motion_entries(trace.entries)), "wall_s": round(wall, 2),
            "final_joints": [round(v, 4) for v in final], "trace_dir": os.path.relpath(trace.dir, self.out_dir),
            "versions": self.versions, "time": datetime.now().isoformat(timespec="seconds"),
        }

    def _run_agent(self, job: Job, env: Env, trace: Trace, allow_motion: bool, answer: str, reset):
        cfg = self.config
        if job.model.provider in CLI_AGENTS:
            from k1_agent.claude_code import ClaudeCodeRunner
            from k1_agent.codex import CodexRunner

            if job.model.provider == "claude-code":
                cc, runner_class, extra = cfg.get("claude_code") or {}, ClaudeCodeRunner, {}
            else:   # Codex reports tokens, not cost
                cc, runner_class, extra = cfg.get("codex") or {}, CodexRunner, {"pricing": self.pricing}
            flags = (["--allow-motion"] if allow_motion else []) + ["--max-steps", str(cfg["max_steps"])]
            runner = runner_class(env.client, trace, env.client.http_url, cfg["config_path"], flags,
                                  model=job.model.model if job.model.model != "default" else None,
                                  command=cc.get("command") or CLI_AGENTS[job.model.provider],
                                  use_api_key=bool(cc.get("use_api_key")), timeout=cc.get("timeout", 900), **extra)
            return runner.run(job.task["prompt"], job.mode)

        from k1_agent.primitives import Primitives
        from k1_agent.runner import AgentRunner
        from k1_agent.skills import SkillLibrary

        with self._lock:
            provider = env.providers.get(job.model) or make_api_provider(job.model)
            env.providers[job.model] = provider
        prims = Primitives(env.client, trace, allow_motion=allow_motion, allow_enable=False,
                           max_steps=cfg["max_steps"], max_step_rad=cfg["max_step_rad"], tolerance=cfg["tolerance"],
                           move_timeout=cfg["move_timeout"])
        operator = AutoOperator(answer, reset) if env.sim is not None else env.web_operator
        runner = AgentRunner(provider, prims, SkillLibrary(self.skills_dir), trace, operator,
                             max_turns=cfg["max_turns"], image_history=cfg["image_history"],
                             max_tokens=cfg["max_tokens"], program_timeout=cfg["program_timeout"],
                             pricing=self.pricing)
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


def run_suite(config: dict, tasks: list[dict], models: list[ModelSpec], out_dir: str, trials: int = 1,
              modes: list[str] | None = None, parallel: int = 1, skills_on: bool = True,
              max_cost_usd: float | None = None, url: str | None = None, seed: int = 0, log=print) -> str:
    """Runs every (task, model, mode, trial) not already in out_dir/results.jsonl. Returns the results path."""
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
    evaluator = Evaluator(config, out_dir, skills_on, max_cost_usd, log)
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
    log(f"spent about ${evaluator.spent:.2f}" + (f"; {evaluator.unpriced} trials had no price (see pricing.yaml)"
                                                  if evaluator.unpriced else ""))
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


# ---------------- report ----------------

def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def load_rows(paths: list[str]) -> list[dict]:
    rows = []
    for path in paths:
        files = [os.path.join(path, "results.jsonl")] if os.path.isdir(path) else [path]
        for file in files:
            with open(file, encoding="utf-8") as f:
                rows += [json.loads(line) for line in f if line.strip()]
    return rows


def summarize_trials(rows: list[dict]) -> list[dict]:
    groups: dict = {}
    for row in rows:
        if row.get("kind") == "trial":
            key = (row["tier"], row["model"], row["mode"], row.get("skills", "on"))
            groups.setdefault(key, []).append(row)
    out = []
    for (tier, model, mode, skills), group in sorted(groups.items()):
        n, wins = len(group), sum(r["success"] for r in group)
        costs = [r.get("cost_usd") for r in group]
        known = None not in costs
        mean_cost = sum(costs) / n if known else None
        low, high = wilson(wins, n)
        out.append({
            "tier": tier, "model": model, "mode": mode, "skills": skills, "n": n, "successes": wins,
            "success_rate": wins / n, "ci_low": low, "ci_high": high, "cost_per_trial": mean_cost,
            "cost_per_success": (sum(costs) / wins if known and wins else None),
            "false_successes": sum(1 for r in group if r.get("claimed") and not r["success"]),
            "errors": sum(1 for r in group if r.get("error")),
            "wall_s": sum(r["wall_s"] for r in group) / n,
            "tasks": {t: (sum(r["success"] for r in group if r["suite_task"] == t),
                          sum(1 for r in group if r["suite_task"] == t))
                      for t in sorted({r["suite_task"] for r in group})},
        })
    for tier in {g["tier"] for g in out}:
        same = [g for g in out if g["tier"] == tier]
        for g in same:
            g["frontier"] = g["cost_per_trial"] is not None and not any(
                o is not g and o["cost_per_trial"] is not None and o["success_rate"] >= g["success_rate"]
                and o["cost_per_trial"] <= g["cost_per_trial"]
                and (o["success_rate"] > g["success_rate"] or o["cost_per_trial"] < g["cost_per_trial"])
                for o in same)
    return out


def summarize_perception(rows: list[dict]) -> list[dict]:
    groups: dict = {}
    for row in rows:
        if row.get("kind") == "perception":
            groups.setdefault(row["model"], []).append(row)
    out = []
    for model, group in sorted(groups.items()):
        n = len(group)
        costs = [r.get("cost_usd") for r in group]
        categories = sorted({r["category"] for r in group})
        out.append({
            "model": model, "n": n, "accuracy": sum(r["correct"] for r in group) / n,
            "abstained": sum(r["abstained"] for r in group) / n,
            "confident_wrong": sum(1 for r in group if not r["correct"] and not r["abstained"]) / n,
            "cost_per_100": (sum(costs) / n * 100) if None not in costs else None,
            "latency_s": sum(r["latency_s"] for r in group) / n,
            "by_category": {c: sum(r["correct"] for r in group if r["category"] == c) /
                            sum(1 for r in group if r["category"] == c) for c in categories},
        })
    return out


def _money(value) -> str:
    return "?" if value is None else (f"${value:.4f}" if value < 1 else f"${value:.2f}")


def report_markdown(trials: list[dict], perception: list[dict]) -> str:
    lines = []
    for tier in sorted({g["tier"] for g in trials}):
        lines += [f"## Tier {tier}: success rate vs cost", "",
                  "| model | mode | skills | success | 95% CI | $ / trial | $ / success | false successes | n |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for g in sorted((g for g in trials if g["tier"] == tier), key=lambda g: -g["success_rate"]):
            star = " *" if g.get("frontier") else ""
            lines.append(f"| {g['model']}{star} | {g['mode']} | {g['skills']} | {g['success_rate']:.0%} | "
                         f"{g['ci_low']:.0%}-{g['ci_high']:.0%} | {_money(g['cost_per_trial'])} | "
                         f"{_money(g['cost_per_success'])} | {g['false_successes']} | {g['n']} |")
        lines += ["", "* = on the frontier: no other model is both at least as successful and at least as cheap.", ""]
        task_ids = sorted({t for g in trials if g["tier"] == tier for t in g["tasks"]})
        lines += ["| model | mode | " + " | ".join(task_ids) + " |", "|---|---|" + "---|" * len(task_ids)]
        for g in (g for g in trials if g["tier"] == tier):
            cells = [f"{g['tasks'][t][0]}/{g['tasks'][t][1]}" if t in g["tasks"] else "-" for t in task_ids]
            lines.append(f"| {g['model']} | {g['mode']} | " + " | ".join(cells) + " |")
        lines.append("")
    if perception:
        categories = sorted({c for p in perception for c in p["by_category"]})
        lines += ["## Tier 0: perception", "",
                  "| model | accuracy | can't tell | confidently wrong | $ / 100 questions | latency | " +
                  " | ".join(categories) + " |", "|---|---|---|---|---|---|" + "---|" * len(categories)]
        for p in sorted(perception, key=lambda p: -p["accuracy"]):
            cells = [f"{p['by_category'][c]:.0%}" if c in p["by_category"] else "-" for c in categories]
            lines.append(f"| {p['model']} | {p['accuracy']:.0%} | {p['abstained']:.0%} | {p['confident_wrong']:.0%} | "
                         f"{_money(p['cost_per_100'])} | {p['latency_s']:.1f} s | " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines) or "No results yet."


def _svg_scatter(groups: list[dict]) -> str:
    """Success rate (y, with 95% CI bars) against $ per trial (x, log scale)."""
    points = [g for g in groups if g["cost_per_trial"]]
    if not points:
        return "<p class='muted'>No priced trials to plot yet.</p>"
    width, height, left, right, top, bottom = 720, 420, 64, 24, 20, 56
    lo = math.log10(min(g["cost_per_trial"] for g in points)) - 0.2
    hi = math.log10(max(g["cost_per_trial"] for g in points)) + 0.2
    if hi - lo < 0.5:
        lo, hi = lo - 0.25, hi + 0.25

    def x(cost):
        return left + (math.log10(cost) - lo) / (hi - lo) * (width - left - right)

    def y(rate):
        return top + (1 - rate) * (height - top - bottom)

    parts = [f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='success rate against cost per trial'>"]
    for rate in (0, 0.25, 0.5, 0.75, 1.0):
        parts.append(f"<line class='grid' x1='{left}' x2='{width - right}' y1='{y(rate):.1f}' y2='{y(rate):.1f}'/>"
                     f"<text class='tick' x='{left - 8}' y='{y(rate) + 4:.1f}' text-anchor='end'>{rate:.0%}</text>")
    for exponent in range(math.floor(lo), math.ceil(hi) + 1):
        for mult in (1, 2, 5):
            cost = mult * 10 ** exponent
            if lo <= math.log10(cost) <= hi:
                parts.append(f"<line class='grid' x1='{x(cost):.1f}' x2='{x(cost):.1f}' y1='{top}' "
                             f"y2='{height - bottom}'/><text class='tick' x='{x(cost):.1f}' y='{height - bottom + 18}' "
                             f"text-anchor='middle'>{_money(cost)}</text>")
    parts.append(f"<text class='axis' x='{(left + width - right) / 2}' y='{height - 12}' text-anchor='middle'>"
                 "average cost per trial (log scale)</text>"
                 f"<text class='axis' transform='rotate(-90)' x='{-(top + height - bottom) / 2}' y='16' "
                 "text-anchor='middle'>success rate (checker)</text>")
    for g in points:
        cx, cy = x(g["cost_per_trial"]), y(g["success_rate"])
        label = g["model"].split(":", 1)[-1] + ("" if g["mode"] == "tools" else f" ({g['mode']})") + \
            ("" if g["skills"] == "on" else " no-skills")
        parts.append(f"<line class='ci' x1='{cx:.1f}' x2='{cx:.1f}' y1='{y(g['ci_low']):.1f}' y2='{y(g['ci_high']):.1f}'/>"
                     f"<circle class='{'dot frontier' if g.get('frontier') else 'dot'}' cx='{cx:.1f}' cy='{cy:.1f}' r='6'>"
                     f"<title>{label}: {g['success_rate']:.0%} at {_money(g['cost_per_trial'])}/trial, n={g['n']}</title>"
                     f"</circle><text class='label' x='{cx + 9:.1f}' y='{cy - 8:.1f}'>{label}</text>")
    parts.append("</svg>")
    return "".join(parts)


def report_html(trials: list[dict], perception: list[dict], title: str = "K1 model evaluation") -> str:
    import html as html_lib

    def md_table_to_html(md: str) -> str:
        out, rows = [], [line for line in md.splitlines()]
        in_table = False
        for line in rows:
            if line.startswith("|"):
                cells = [html_lib.escape(c.strip()) for c in line.strip("|").split("|")]
                if set(line) <= set("|-"):
                    continue
                if not in_table:
                    out.append("<div class='scroll'><table><thead><tr>" + "".join(f"<th>{c}</th>" for c in cells) +
                               "</tr></thead><tbody>")
                    in_table = True
                else:
                    out.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
            else:
                if in_table:
                    out.append("</tbody></table></div>")
                    in_table = False
                if line.startswith("## "):
                    out.append(f"<h2>{html_lib.escape(line[3:])}</h2>")
                elif line.strip():
                    out.append(f"<p class='muted'>{html_lib.escape(line)}</p>")
        if in_table:
            out.append("</tbody></table></div>")
        return "\n".join(out)

    body = []
    for tier in sorted({g["tier"] for g in trials}):
        body.append(md_table_to_html(report_markdown([g for g in trials if g["tier"] == tier], [])))
        body.append(f"<figure>{_svg_scatter([g for g in trials if g['tier'] == tier])}</figure>")
    if perception:
        body.append(md_table_to_html(report_markdown([], perception)))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_lib.escape(title)}</title>
<style>
:root {{ --bg: #fbfbfa; --fg: #1f2328; --muted: #6a6f76; --line: #e3e4e6; --accent: #2f6fdb; --star: #d97706; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg: #16181c; --fg: #e8e9eb;
  --muted: #9aa0a8; --line: #2c2f35; --accent: #6ea0ff; --star: #f5a524; }} }}
:root[data-theme="dark"] {{ --bg: #16181c; --fg: #e8e9eb; --muted: #9aa0a8; --line: #2c2f35; --accent: #6ea0ff;
  --star: #f5a524; }}
body {{ background: var(--bg); color: var(--fg); font: 15px/1.5 system-ui, sans-serif; margin: 0 auto;
  max-width: 1000px; padding: 24px 16px 48px; }}
h1 {{ font-size: 1.5rem; margin: 0 0 4px; }} h2 {{ font-size: 1.15rem; margin: 32px 0 8px; }}
.muted {{ color: var(--muted); font-size: 0.9rem; }}
.scroll {{ overflow-x: auto; }} table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
th, td {{ border-bottom: 1px solid var(--line); padding: 6px 10px; text-align: left; white-space: nowrap; }}
th {{ font-weight: 600; }}
figure {{ margin: 16px 0; }} svg {{ width: 100%; height: auto; }}
.grid {{ stroke: var(--line); }} .tick, .label {{ fill: var(--muted); font-size: 11px; }}
.axis {{ fill: var(--fg); font-size: 12px; }} .ci {{ stroke: var(--muted); stroke-width: 1.5; }}
.dot {{ fill: var(--accent); }} .dot.frontier {{ fill: var(--star); }}
</style></head><body>
<h1>{html_lib.escape(title)}</h1>
<p class="muted">Success is decided by each task's checker, not by the model's own claim. Costs are from token
counts and pricing.yaml (Claude Code: its own API-equivalent estimate). Generated {datetime.now():%Y-%m-%d %H:%M}.</p>
{''.join(body)}
</body></html>
"""
