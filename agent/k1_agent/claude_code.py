"""
Claude Code as the agent's model, signed in with a Claude account (`k1-agent login`) instead of an
API key.

Each task runs one headless Claude Code session:

    claude -p TASK --output-format stream-json --mcp-config {k1: k1-agent mcp ...}
           --tools "" --allowedTools mcp__k1 --permission-mode dontAsk

- Its only tools are the k1 MCP tools (`--tools ""` turns off Bash, file edits and web access), so
  it moves the arm with the same tools, interlocks and step budget as `k1-agent run`, and gets the
  same operating rules (AGENTS.md) as its system prompt.
- It finishes by calling the `done(success, summary)` tool, which this runner reads from the event
  stream. A session that ends without calling done counts as a failure (reason `no_done`).
- Questions go to the web page (ask_operator through WebOperator).
- A STOP on the web page ends the session: the MCP server's primitives refuse, and the process is
  terminated.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time

from k1_agent.client import K1Client
from k1_agent.prompts import CLI_NOTE, operating_rules
from k1_agent.providers.base import Usage
from k1_agent.runner import RunResult
from k1_agent.trace import Trace


def said_success(value) -> bool:
    """The `success` of a done call, read from the raw call: a model can send the string "false", which is truthy."""
    return value.strip().lower() == "true" if isinstance(value, str) else bool(value)

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ClaudeCodeError(Exception):
    pass


def find_claude(command: str = "claude") -> str:
    path = shutil.which(command)
    if not path:
        raise ClaudeCodeError(f"Claude Code ({command!r}) is not installed or not on PATH. Install it from "
                              "https://claude.com/claude-code, then run `k1-agent login`")
    return path


def login_status(command: str = "claude") -> dict:
    """`claude auth status` as a dict, e.g. {"loggedIn": true, "authMethod": "claude.ai", ...}."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):        # from `claude setup-token`, for headless PCs
        return {"loggedIn": True, "authMethod": "oauth_token"}
    try:
        out = subprocess.run([find_claude(command), "auth", "status", "--json"], capture_output=True,
                             text=True, timeout=30)
        return json.loads(out.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        raise ClaudeCodeError(f"could not read the Claude Code login status: {e}") from e


def login(command: str = "claude") -> int:
    """Interactive sign-in with a Claude account (opens a browser, or prints a link to open)."""
    return subprocess.call([find_claude(command), "auth", "login"])


def system_rules() -> str:
    """The shared operating rules, plus the reminder that a CLI agent has only the k1 tools here."""
    return operating_rules() + "\n" + CLI_NOTE


class ClaudeCodeRunner:
    """Runs tasks through the Claude Code CLI. `mcp_args` are the `k1-agent mcp` safety flags;
    `command` is the CLI's name, or a full argument list to launch it with."""

    name = "claude-code"
    label = "Claude Code"

    def __init__(self, client: K1Client, trace: Trace, url: str, config_path: str | None = None,
                 mcp_args: list[str] | None = None, model: str | None = None, command: str = "claude",
                 use_api_key: bool = False, max_budget_usd: float | None = None, timeout: float = 900.0,
                 effort: str | None = None):
        self.client = client
        self.trace = trace
        self.url = url
        self.config_path = config_path
        self.mcp_args = mcp_args or []
        self.model = model
        self.command = command
        self.use_api_key = use_api_key
        self.max_budget_usd = max_budget_usd
        self.timeout = timeout
        self.effort = effort                    # low | medium | high | xhigh | max; None = the CLI's default
        self.usage: Usage | None = None
        self.cost_usd: float | None = None      # Claude Code's own estimate, at API prices
        self.turns = 0
        self.error: str | None = None           # an error the CLI reported in its event stream
        self.outcome: tuple[bool, str] | None = None    # from the agent's done(success, summary) call

    def mcp_config(self) -> str:
        args = ["-m", "k1_agent.cli", "--url", self.url]
        if self.config_path:
            args += ["--config", self.config_path]
        args += ["mcp", "--web-operator"] + self.mcp_args
        return json.dumps({"mcpServers": {"k1": {"command": sys.executable, "args": args}}})

    def command_line(self, task: str, mode: str = "tools") -> list[str]:
        prompt = f"Task for the K1 arm: {task}"
        if mode == "code":
            prompt += ("\n\nWork in short, planned sequences: say your plan in a note first, then carry it out, "
                       "checking the frame each motion tool returns.")
        launcher = list(self.command) if isinstance(self.command, (list, tuple)) else [find_claude(self.command)]
        cmd = launcher + ["-p", prompt, "--output-format", "stream-json", "--verbose",
               "--mcp-config", self.mcp_config(), "--strict-mcp-config", "--tools", "",
               "--allowedTools", "mcp__k1", "--permission-mode", "dontAsk",
               "--append-system-prompt", system_rules(), "--no-session-persistence"]
        if self.model:
            cmd += ["--model", self.model]
        if self.effort:
            cmd += ["--effort", self.effort]
        if self.max_budget_usd:
            cmd += ["--max-budget-usd", str(self.max_budget_usd)]
        return cmd

    def environment(self) -> dict:
        env = dict(os.environ, K1_WEB_TOKEN=self.client.token)     # for the MCP server Claude Code starts
        env.pop("K1_WEB_AGENT_TOKEN", None)     # so it signs in with exactly this runner's token
        if not self.use_api_key:
            # with an API key in the environment Claude Code bills the API instead of the signed-in account
            env.pop("ANTHROPIC_API_KEY", None)
        return env

    def note(self, text: str):
        try:
            self.client.note(text[:300])
        except Exception:
            pass

    def tool_call(self, name: str, arguments):
        """Trace and narrate one of the agent's tool calls; `done` ends the task with its verdict."""
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {"raw": arguments}
        arguments = arguments if isinstance(arguments, dict) else {}
        self.trace.log("tool_call", name=name, arguments=arguments)
        if name == "done":
            self.outcome = (said_success(arguments.get("success")), str(arguments.get("summary", "")))
        elif name != "note":
            self.note(f"-> {name}({json.dumps(arguments)[:200]})")

    def handle_event(self, event: dict) -> str | None:
        """Trace and narrate one stream-json event. Returns the final text for the result event."""
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            self.trace.log("claude_code_init", model=event.get("model"), tools=event.get("tools"))
        elif kind == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.trace.log("llm", text=block["text"])
                    self.note(block["text"].strip())
                elif block.get("type") == "tool_use":
                    self.tool_call(block.get("name", "").removeprefix("mcp__k1__"), block.get("input"))
        elif kind == "result":
            usage = event.get("usage") or {}
            self.usage = Usage(input=usage.get("input_tokens") or 0, output=usage.get("output_tokens") or 0,
                               cache_read=usage.get("cache_read_input_tokens") or 0,
                               cache_write=usage.get("cache_creation_input_tokens") or 0)
            self.cost_usd = event.get("total_cost_usd")
            self.turns = event.get("num_turns") or 0
            self.trace.log("claude_code_result", subtype=event.get("subtype"), is_error=event.get("is_error"),
                           cost_usd=self.cost_usd, turns=self.turns, usage=self.usage.as_dict())
            return event.get("result") or ""
        return None

    def run(self, task: str, mode: str = "tools") -> RunResult:
        self.outcome = None
        self.trace.log("task", task=task, mode=mode, provider=self.name, model=self.model)
        proc = subprocess.Popen(self.command_line(task, mode), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                env=self.environment(), cwd=AGENT_DIR)
        ended = []          # why the watcher ended the session: "stopped" or "timeout"

        def watch():
            deadline = time.monotonic() + self.timeout
            while proc.poll() is None:
                if self.client.stopped or time.monotonic() > deadline:
                    ended.append("stopped" if self.client.stopped else "timeout")
                    proc.terminate()
                    return
                time.sleep(0.2)

        threading.Thread(target=watch, daemon=True).start()
        stderr_lines: list[str] = []
        threading.Thread(target=lambda: stderr_lines.extend(proc.stderr), daemon=True).start()

        final = None
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            text = self.handle_event(event)
            if text is not None:
                final = text
        proc.wait()

        if ended == ["stopped"]:
            result = RunResult(False, "the operator pressed STOP", "stopped", self.trace.dir)
        elif ended == ["timeout"]:
            result = RunResult(False, f"no result after {self.timeout:.0f} s", "timeout", self.trace.dir)
        elif self.outcome is not None:
            success, summary = self.outcome
            result = RunResult(success, summary, "done", self.trace.dir)
        elif final is None:
            detail = self.error or "".join(stderr_lines).strip()[-500:] or f"exit code {proc.returncode}"
            result = RunResult(False, f"{self.label} failed: {detail}", "error", self.trace.dir)
        else:
            result = RunResult(False, f"ended without calling done: {final.strip()[-300:]}", "no_done",
                               self.trace.dir)
        result.usage, result.cost_usd, result.llm_calls = self.usage, self.cost_usd, self.turns
        self.trace.log("outcome", success=result.success, summary=result.summary, reason=result.reason,
                       usage=self.usage.as_dict() if self.usage else None, cost_usd=self.cost_usd,
                       llm_calls=self.turns)
        self.note(f"{'SUCCESS' if result.success else 'FAILED'} ({result.reason}): {result.summary[:200]}")
        return result
