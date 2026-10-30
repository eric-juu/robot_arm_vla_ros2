"""
OpenAI's Codex CLI as the agent's model, signed in with a ChatGPT account (`codex login`) instead of an
API key: the Codex counterpart of claude_code.py.

Each task runs one headless Codex session:

    codex exec --json --ephemeral --ignore-user-config -s read-only -c approval_policy="never"
               -c mcp_servers.k1={k1-agent mcp ...} --disable shell_tool ... PROMPT

Codex has no switch that leaves only the MCP tools, so (checked with codex-cli 0.166.0):
- its own tools are turned off one by one (DISABLED_FEATURES: shell, browser, computer use, plugins, ...);
- what remains is fenced in: a read-only sandbox in an empty folder (apply_patch is refused), code mode's
  runtime can't import node modules (no files, no processes), and a sub-agent can't start in an
  --ephemeral session;
- only the k1 server's tools are pre-approved; with approval_policy "never" anything else needing
  approval is refused.
Any use of a tool other than k1's is logged to the trace as `codex_item`.

Codex needs `codex-code-mode-host` next to it (it ships separately in the GitHub release): without it
the k1 tools never reach the model.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

from k1_agent.claude_code import ClaudeCodeRunner, operating_rules
from k1_agent.providers.base import Usage

# Codex features that give the model tools besides the k1 ones (`codex features list`)
DISABLED_FEATURES = ["shell_tool", "unified_exec", "browser_use", "browser_use_external", "in_app_browser",
                     "computer_use", "apps", "plugins", "remote_plugin", "multi_agent", "multi_agent_v2",
                     "image_generation", "view_image", "tool_suggest", "hooks", "goals", "memories", "sleep_tool",
                     "skill_mcp_dependency_install",
                     "incremental_tools"]       # not a tool: with it on, MCP tools can arrive after the first turn


class CodexError(Exception):
    pass


def find_codex(command: str = "codex") -> str:
    path = shutil.which(command)
    if not path:
        raise CodexError(f"Codex ({command!r}) is not installed or not on PATH. Install it from "
                         "https://github.com/openai/codex, then run `codex login`")
    return path


def login_status(command: str = "codex") -> dict:
    """{"loggedIn": bool, "method": "ChatGPT" | "API key" | None} from `codex login status`."""
    try:
        out = subprocess.run([find_codex(command), "login", "status"], capture_output=True, text=True,
                             timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CodexError(f"could not read the Codex login status: {e}") from e
    text = (out.stdout + out.stderr).strip()
    if out.returncode != 0 or "not logged in" in text.lower():
        return {"loggedIn": False, "method": None}
    return {"loggedIn": True, "method": "ChatGPT" if "chatgpt" in text.lower() else "API key"}


class CodexRunner(ClaudeCodeRunner):
    """Runs tasks through the Codex CLI. Same arguments as ClaudeCodeRunner, plus `pricing` to estimate the
    cost from Codex's token counts (Codex reports no cost of its own)."""

    name = "codex"
    label = "Codex"

    def __init__(self, *args, command: str = "codex", pricing=None, **kwargs):
        super().__init__(*args, command=command, **kwargs)
        self.pricing = pricing
        self.workdir: str | None = None
        self.last_message: str | None = None
        self.tool_calls = 0

    def mcp_overrides(self) -> list[str]:
        args = ["-m", "k1_agent.cli", "--url", self.url]
        if self.config_path:        # Codex starts the server in its own (empty) working folder
            args += ["--config", os.path.abspath(self.config_path)]
        args += ["mcp", "--web-operator"] + self.mcp_args
        # required: wait for the server before the first turn, and fail if it doesn't start (otherwise the
        # model can begin without the k1 tools, e.g. when several sessions start at once)
        server = {"command": sys.executable, "args": args, "env": {"K1_WEB_TOKEN": self.client.token},
                  "required": True, "default_tools_approval_mode": "approve", "startup_timeout_sec": 60,
                  # ask_operator waits for a person on the web page
                  "tool_timeout_sec": max(int(self.timeout), 60)}
        overrides = []
        for key, value in server.items():
            overrides += ["-c", f"mcp_servers.k1.{key}={toml_table(value) if isinstance(value, dict) else json.dumps(value)}"]
        return overrides

    def command_line(self, task: str, mode: str = "tools") -> list[str]:
        prompt = f"{operating_rules()}\n\nTask for the K1 arm: {task}"
        if mode == "code":
            prompt += ("\n\nWork in short, planned sequences: say your plan in a note first, then carry it out, "
                       "checking the frame each motion tool returns.")
        launcher = list(self.command) if isinstance(self.command, (list, tuple)) else [find_codex(self.command)]
        cmd = launcher + ["exec", "--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
                          "--ignore-rules", "-s", "read-only", "-C", self.workdir or tempfile.gettempdir(),
                          "-c", 'approval_policy="never"', "-c", 'web_search="disabled"'] + self.mcp_overrides()
        for feature in DISABLED_FEATURES:
            cmd += ["--disable", feature]
        if self.model:
            cmd += ["-m", self.model]
        return cmd + ["--", prompt]

    def environment(self) -> dict:
        env = dict(os.environ, K1_WEB_TOKEN=self.client.token)
        if not self.use_api_key:
            # bill the signed-in ChatGPT account, not an API key that happens to be set
            env.pop("OPENAI_API_KEY", None)
            env.pop("CODEX_API_KEY", None)
        return env

    def handle_event(self, event: dict) -> str | None:
        """Trace and narrate one `codex exec --json` event. Returns the final text when the turn ends."""
        kind = event.get("type")
        item = event.get("item") or {}
        if kind == "thread.started":
            self.trace.log("codex_init", model=self.model, thread_id=event.get("thread_id"))
        elif kind == "item.started" and item.get("type") == "mcp_tool_call":
            self.tool_calls += 1
            name = item.get("tool", "") if item.get("server") == "k1" else f"{item.get('server')}.{item.get('tool')}"
            self.trace.log("tool_call", name=name, arguments=item.get("arguments"))
            if name != "note":
                self.note(f"-> {name}({json.dumps(item.get('arguments'))[:200]})")
        elif kind == "item.completed" and item.get("type") == "agent_message":
            text = item.get("text", "")
            if text.strip():
                self.last_message = text
                self.trace.log("llm", text=text)
                self.note(text.strip())
        elif kind == "item.completed" and item.get("type") == "error":
            self.trace.log("codex_error", message=item.get("message"))
        elif kind == "item.completed" and item.get("type") not in ("mcp_tool_call", "reasoning"):
            # anything other than k1 tools and text (a patch, a sub-agent...): keep a record of it
            self.trace.log("codex_item", item={k: v for k, v in item.items() if k != "id"})
        elif kind == "turn.completed":
            usage = event.get("usage") or {}
            cached = usage.get("cached_input_tokens") or 0
            self.usage = Usage(input=(usage.get("input_tokens") or 0) - cached, output=usage.get("output_tokens") or 0,
                               cache_read=cached, cache_write=usage.get("cache_write_input_tokens") or 0,
                               reasoning=usage.get("reasoning_output_tokens") or 0)
            self.cost_usd = self.pricing.cost(self.model, self.usage) if self.pricing else None
            self.turns = self.tool_calls + 1        # about one model call per tool call, plus the answer
            self.trace.log("codex_result", cost_usd=self.cost_usd, usage=self.usage.as_dict())
            return self.last_message or ""
        elif kind in ("turn.failed", "error"):
            self.error = ((event.get("error") or {}).get("message") or event.get("message") or "")[:500]
            self.trace.log("codex_error", message=self.error)
        return None

    def run(self, task: str, mode: str = "tools"):
        with tempfile.TemporaryDirectory(prefix="k1-codex-") as workdir:
            self.workdir = workdir
            return super().run(task, mode)


def toml_table(value: dict) -> str:
    """{"K1_WEB_TOKEN": "x"} -> '{K1_WEB_TOKEN = "x"}' for a `-c key=value` override."""
    return "{" + ", ".join(f"{k} = {json.dumps(v)}" for k, v in value.items()) + "}"
