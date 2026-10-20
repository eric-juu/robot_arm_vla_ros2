"""
Runs code-as-policy programs written by the model, ASPIRE-style.

A program is Python that defines `run(k1)` and uses only the primitive API on `k1`:

    def run(k1):
        k1.go_home()
        k1.move_joint_delta("shoulder_pan", 0.2)
        return k1.observe()["joints"]

This is a guardrail, not a security boundary. The program runs in a child process with a
minimal environment (no API keys, no k1_web token) and restricted builtins. Imports other than
`math` and names starting with "_" are rejected before it starts. It is killed after `timeout`.
The real protection is that the program can only act through the primitives, which go through
k1_web's limits and the run's interlocks. ASPIRE gives the same warning about generated code.
"""
import ast
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field

from k1_agent.client import StoppedByOperator

CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sandbox_child.py")
ALLOWED_MODULES = {"math"}


@dataclass
class ProgramResult:
    ok: bool
    returned: object = None
    error: str | None = None
    output: list[str] = field(default_factory=list)
    calls: int = 0


def check_program(code: str) -> str | None:
    """Static checks before running. Returns an error message, or None if the program looks acceptable."""
    try:
        tree = ast.parse(code, "program.py")
    except SyntaxError as e:
        return f"syntax error on line {e.lineno}: {e.msg}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad = [a.name for a in node.names if a.name not in ALLOWED_MODULES]
            if bad:
                return f"line {node.lineno}: cannot import {bad}; only {sorted(ALLOWED_MODULES)} and the k1 API"
        elif isinstance(node, ast.ImportFrom):
            if node.module not in ALLOWED_MODULES:
                return f"line {node.lineno}: cannot import from {node.module!r}"
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            return f"line {node.lineno}: attributes starting with '_' are not allowed"
        elif isinstance(node, ast.Name) and node.id.startswith("__"):
            return f"line {node.lineno}: names starting with '__' are not allowed"
    if not any(isinstance(n, ast.FunctionDef) and n.name == "run" for n in tree.body):
        return "the program must define a top-level function run(k1)"
    return None


def jsonable(value):
    """Primitive results without image bytes (the frames are already in the trace)."""
    if isinstance(value, dict):
        return {k: ("<frame saved in trace>" if isinstance(v, bytes) else jsonable(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def run_program(code: str, dispatch, api: list[str], timeout: float = 120.0) -> ProgramResult:
    """Run `code`, executing each k1.<name>(...) call with dispatch(name, args, kwargs).
    Raises StoppedByOperator if STOP was pressed while it ran."""
    problem = check_program(code)
    if problem:
        return ProgramResult(False, error=problem)

    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT") if k in os.environ}
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.Popen([sys.executable, "-I", CHILD], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env, cwd=cwd)
        lines: queue.Queue = queue.Queue()

        def read():
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=read, daemon=True).start()
        result = ProgramResult(False)
        stopped = False

        def reply(obj):
            try:
                proc.stdin.write(json.dumps(obj) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

        reply({"code": code, "api": api})
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    line = lines.get(timeout=max(deadline - time.monotonic(), 0.0))
                except queue.Empty:
                    result.error = f"timed out after {timeout:.0f} s"
                    break
                if line is None:
                    result.error = "the program process exited unexpectedly: " + proc.stderr.read()[-2000:]
                    break
                msg = json.loads(line)
                if "print" in msg:
                    result.output.append(msg["print"])
                elif "call" in msg:
                    result.calls += 1
                    if stopped:
                        reply({"stopped": True})
                        continue
                    name = msg["call"]
                    if name not in api:
                        value = {"ok": False, "error": "bad_arguments", "message": f"k1 has no primitive {name!r}"}
                    else:
                        try:
                            value = dispatch(name, msg.get("args", []), msg.get("kwargs", {}))
                        except StoppedByOperator:
                            stopped = True
                            reply({"stopped": True})
                            continue
                        except TypeError as e:
                            value = {"ok": False, "error": "bad_arguments", "message": str(e)}
                    reply({"result": jsonable(value)})
                elif msg.get("done"):
                    result.ok = not msg.get("error") and not msg.get("stopped")
                    result.error = msg.get("error")
                    result.returned = msg.get("returned")
                    break
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                stream.close()
    if stopped:
        raise StoppedByOperator("STOP was pressed while the program ran")
    return result
