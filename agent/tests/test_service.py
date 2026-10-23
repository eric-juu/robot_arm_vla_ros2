import json
import sys
import textwrap
import threading
import time

from websockets.sync.client import connect

from k1_agent.claude_code import ClaudeCodeRunner, parse_result
from k1_agent.client import K1Client
from k1_agent.primitives import Primitives
from k1_agent.providers.base import Provider, Reply, ToolCall
from k1_agent.runner import AgentRunner
from k1_agent.service import AgentService
from k1_agent.skills import SkillLibrary
from k1_agent.trace import Trace


class ScriptedProvider(Provider):
    name = "fake"

    def __init__(self, replies):
        super().__init__("scripted")
        self.replies = list(replies)

    def chat(self, system, messages, tools, max_tokens=4096):
        return self.replies.pop(0)


class Page:
    """A human's browser tab, over a raw WebSocket."""

    def __init__(self, live_server):
        self.ws = connect(live_server["url"].replace("http", "ws") + "/ws",
                          additional_headers={"Authorization": f"Bearer {live_server['token']}"}).__enter__()
        self.ws.send(json.dumps({"type": "hello", "role": "human"}))

    def send(self, msg):
        self.ws.send(json.dumps(msg))

    def receive(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = json.loads(self.ws.recv(timeout=deadline - time.monotonic()))
            if predicate(msg):
                return msg
        raise AssertionError("message never arrived")

    def agent_state(self, state):
        return self.receive(lambda m: m["type"] == "state" and (m.get("agent") or {}).get("state") == state)


def test_prompt_from_the_page_runs_a_task_and_asks_questions(live_server, tmp_path):
    page = Page(live_server)
    with K1Client(live_server["url"], live_server["token"]) as k1:
        assert Primitives(k1, allow_enable=True).enable()["ok"]
        provider = ScriptedProvider([
            Reply("", [ToolCall("1", "ask_operator", {"question": "Where is the cup?"})]),
            Reply("", [ToolCall("2", "move_joint_delta", {"joint": "shoulder_pan", "delta": 0.1})]),
            Reply("Done.", [ToolCall("3", "done", {"success": True, "summary": "turned toward the cup"})]),
        ])
        results = []

        def run_task(text, mode, operator):
            trace = Trace(live_server["runs"], text)
            prims = Primitives(k1, trace, allow_motion=True)
            runner = AgentRunner(provider, prims, SkillLibrary(str(tmp_path / "skills")), trace, operator)
            result = runner.run_tools(text)
            results.append((text, mode, result, operator.confirm("Save a skill draft?")))
            return result

        service = AgentService(k1, run_task, {"provider": "fake", "model": "scripted", "allow_motion": True})
        thread = threading.Thread(target=service.serve_forever, kwargs={"max_tasks": 1}, daemon=True)
        thread.start()

        status = page.agent_state("idle")["agent"]
        assert status == {"state": "idle", "task": "", "provider": "fake", "model": "scripted", "allow_motion": True}
        page.send({"type": "agent_prompt", "text": "turn to the cup", "mode": "tools", "id": "prompt"})
        assert page.receive(lambda m: m.get("id") == "prompt")["type"] == "ack"

        question = page.receive(lambda m: m["type"] == "agent_question")
        assert question["text"] == "Where is the cup?" and question["kind"] == "ask"
        page.send({"type": "agent_answer", "qid": question["qid"], "text": "on the left"})

        confirm = page.receive(lambda m: m["type"] == "agent_question")
        assert confirm["kind"] == "confirm"
        page.send({"type": "agent_answer", "qid": confirm["qid"], "text": "no"})

        thread.join(timeout=10)
        assert not thread.is_alive()
        text, mode, result, saved = results[0]
        assert (text, mode, result.success, saved) == ("turn to the cup", "tools", True, False)
        assert page.agent_state("idle")["agent"]["task"] == ""
    page.ws.close()


def test_stop_before_a_task_does_not_cancel_the_next_one(live_server):
    page = Page(live_server)
    with K1Client(live_server["url"], live_server["token"]) as k1:
        page.send({"type": "stop"})
        page.receive(lambda m: m["type"] == "event")
        time.sleep(0.2)
        assert k1.stopped
        seen = []
        service = AgentService(k1, lambda text, mode, op: seen.append(k1.stopped), {"provider": "fake"})
        thread = threading.Thread(target=service.serve_forever, kwargs={"max_tasks": 1}, daemon=True)
        thread.start()
        page.agent_state("idle")
        page.send({"type": "agent_prompt", "text": "wave"})
        thread.join(timeout=10)
        assert seen == [False]
    page.ws.close()


FAKE_CLAUDE = textwrap.dedent("""
    import json, os, sys, time
    with open(os.environ["FAKE_CLAUDE_LOG"], "w") as f:
        json.dump({"argv": sys.argv[1:], "token": os.environ.get("K1_WEB_TOKEN"),
                   "api_key": os.environ.get("ANTHROPIC_API_KEY")}, f)
    if "sleep" in sys.argv[2]:
        time.sleep(60)
    events = [
        {"type": "system", "subtype": "init", "model": "claude-test", "tools": ["mcp__k1__observe"]},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Looking first."},
            {"type": "tool_use", "id": "t1", "name": "mcp__k1__observe", "input": {}}]}},
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "total_cost_usd": 0,
         "result": "The base is turned.\\nRESULT: success - turned the base left"},
    ]
    for event in events:
        print(json.dumps(event), flush=True)
""")


def make_claude_runner(live_server, tmp_path, k1, monkeypatch):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE)
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude_args.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-passed")
    return ClaudeCodeRunner(k1, Trace(live_server["runs"], "cc"), live_server["url"], None,
                            ["--allow-motion", "--max-steps", "5"], model="sonnet",
                            command=[sys.executable, str(script)])


def test_claude_code_runner(live_server, tmp_path, monkeypatch):
    page = Page(live_server)
    with K1Client(live_server["url"], live_server["token"]) as k1:
        runner = make_claude_runner(live_server, tmp_path, k1, monkeypatch)
        result = runner.run("turn the base left")
        assert (result.success, result.reason, result.summary) == (True, "done", "turned the base left")

        seen = json.loads((tmp_path / "claude_args.json").read_text())
        argv = seen["argv"]
        assert seen["token"] == live_server["token"] and seen["api_key"] is None
        assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--allowedTools") + 1] == "mcp__k1"
        assert argv[argv.index("--model") + 1] == "sonnet"
        server = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["k1"]
        assert server["args"][-4:] == ["--web-operator", "--allow-motion", "--max-steps", "5"]
        assert "RESULT:" in argv[argv.index("--append-system-prompt") + 1]

        notes = [page.receive(lambda m: m["type"] == "agent_note")["text"] for _ in range(3)]
        assert notes[:2] == ["Looking first.", "-> observe({})"] and notes[2].startswith("SUCCESS")
        kinds = [json.loads(line)["kind"] for line in open(runner.trace.path)]
        assert {"task", "claude_code_init", "llm", "tool_call", "claude_code_result", "outcome"} <= set(kinds)
    page.ws.close()


def test_claude_code_runner_ends_on_stop(live_server, tmp_path, monkeypatch):
    page = Page(live_server)
    with K1Client(live_server["url"], live_server["token"]) as k1:
        runner = make_claude_runner(live_server, tmp_path, k1, monkeypatch)
        threading.Timer(1.0, lambda: page.send({"type": "stop"})).start()
        start = time.monotonic()
        result = runner.run("sleep forever")
        assert result.reason == "stopped" and time.monotonic() - start < 10
    page.ws.close()


def test_parse_result():
    assert parse_result("ok\nRESULT: success - picked it up") == (True, "picked it up")
    assert parse_result("**RESULT: failed** the cup fell")[0] is False
    assert parse_result("I could not see the cup.") == (False, "I could not see the cup.")
