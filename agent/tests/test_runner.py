import json
import os

import pytest

from k1_agent.client import K1Client
from k1_agent.primitives import Primitives
from k1_agent.providers.base import Provider, Reply, ToolCall
from k1_agent.runner import AgentRunner, extract_code, prune_images
from k1_agent.skills import SkillLibrary
from k1_agent.trace import Trace


class FakeProvider(Provider):
    """Replays scripted replies and remembers what it was shown."""
    name = "fake"

    def __init__(self, replies):
        super().__init__("scripted")
        self.replies = list(replies)
        self.seen = []

    def chat(self, system, messages, tools, max_tokens=4096):
        self.seen.append({"system": system, "messages": list(messages), "tools": [t.name for t in tools]})
        return self.replies.pop(0)


class ScriptedOperator:
    def __init__(self):
        self.resets = 0

    def ask(self, question):
        return "the cup is on the left"

    def confirm(self, question):
        return True

    def wait_for_reset(self, message):
        self.resets += 1


def call(tool, **arguments):
    return ToolCall(f"id-{tool}", tool, arguments)


@pytest.fixture
def setup(live_server, tmp_path):
    skills = SkillLibrary(str(tmp_path / "skills"))
    os.makedirs(skills.root)
    with open(os.path.join(skills.root, "k1-basics.md"), "w") as f:
        f.write("---\nname: k1-basics\ndescription: joint conventions\n---\nshoulder_pan turns the base.\n")
    with K1Client(live_server["url"], live_server["token"]) as k1:
        prims = Primitives(k1, Trace(live_server["runs"], "run"), allow_motion=True, allow_enable=True)
        assert prims.enable()["ok"]
        yield prims, skills


def make_runner(prims, skills, replies):
    provider = FakeProvider(replies)
    runner = AgentRunner(provider, prims, skills, prims.trace, ScriptedOperator(), max_turns=6)
    return runner, provider


def test_tool_loop(setup):
    prims, skills = setup
    runner, provider = make_runner(prims, skills, [
        Reply("Reading the basics first.", [call("read_skill", name="k1-basics")]),
        Reply("", [call("move_joint_delta", joint="shoulder_pan", delta=0.1)]),
        Reply("", [call("ask_operator", question="where is the cup?")]),
        Reply("Turned left.", [call("done", success=True, summary="turned the base")]),
    ])
    result = runner.run_tools("turn the base left a little")
    assert result.success and result.reason == "done" and result.summary == "turned the base"

    first = provider.seen[0]
    assert "k1-basics: joint conventions" in first["system"]
    assert {"observe", "move_joints", "enable", "done"} <= set(first["tools"])
    assert first["messages"][0]["content"][-1]["type"] == "image"            # initial frame

    move_result = provider.seen[2]["messages"][-1]["results"][0]
    assert json.loads(move_result["content"][0]["text"])["ok"]
    assert move_result["content"][1]["type"] == "image"                     # frame after the move
    answer = provider.seen[3]["messages"][-1]["results"][0]["content"][0]["text"]
    assert "the cup is on the left" in answer

    kinds = [json.loads(line)["kind"] for line in open(prims.trace.path)]
    assert kinds[-1] == "outcome" and "llm" in kinds and "operator" in kinds


def test_tool_loop_turn_limit(setup):
    prims, skills = setup
    runner, _ = make_runner(prims, skills, [Reply("thinking...")] * 6)
    result = runner.run_tools("do nothing")
    assert not result.success and result.reason == "max_turns"


def test_code_mode_with_repair(setup):
    prims, skills = setup
    bad = "```python\ndef run(k1):\n    import os\n```"
    good = ("Plan: turn the base.\n```python\ndef run(k1):\n"
            "    r = k1.move_joint_delta('shoulder_pan', 0.1)\n"
            "    print('moved', r['ok'])\n"
            "    return r['joints']['shoulder_pan']\n```")
    runner, provider = make_runner(prims, skills, [
        Reply(bad),
        Reply("", [call("done", success=False, summary="program was rejected")]),
        Reply(good),
        Reply("", [call("done", success=True, summary="base turned")]),
        Reply("---\nname: turn-base\ndescription: turning the base\n---\nUse move_joint_delta on shoulder_pan.\n"),
    ])
    result = runner.run_code("turn the base", attempts=2)
    assert result.success and runner.operator.resets == 1

    report = provider.seen[3]["messages"][-1]["content"][0]["text"]
    assert "Attempt 2 finished. Program completed." in report and "moved True" in report
    assert "move_joint_delta" in report
    assert os.path.exists(os.path.join(prims.trace.dir, "program_2.py"))

    path = runner.draft_skill("turn the base")
    assert path.endswith(os.path.join("pending", "turn-base.md"))
    assert skills.pending()[0] == {"name": "turn-base", "description": "turning the base"}
    skills.approve("turn-base")
    assert "turn-base" in skills.index_text()


def test_helpers():
    assert extract_code("x\n```python\nprint(1)\n```\n") == "print(1)"
    assert extract_code("no code") is None
    img = {"type": "image", "jpeg": b"x"}
    messages = [{"role": "user", "content": [img]},
                {"role": "tool", "results": [{"id": "1", "name": "observe", "content": [dict(img)]}]},
                {"role": "user", "content": [dict(img)]}]
    prune_images(messages, keep=2)
    assert messages[0]["content"][0]["type"] == "text"
    assert messages[1]["results"][0]["content"][0]["type"] == "image"


def test_pruning_drops_thinking_signed_against_old_frames():
    img = {"type": "image", "jpeg": b"x"}
    thinking = {"type": "thinking", "thinking": "...", "signature": "sig"}
    tool_use = {"type": "tool_use", "id": "1", "name": "observe", "input": {}}
    early = Reply("", [], [thinking, tool_use])
    late = Reply("", [], [dict(thinking), dict(tool_use)])
    messages = [{"role": "user", "content": [img]},
                {"role": "assistant", "reply": early},
                {"role": "tool", "results": [{"id": "1", "name": "observe", "content": [dict(img)]}]},
                {"role": "assistant", "reply": late},
                {"role": "tool", "results": [{"id": "1", "name": "observe", "content": [dict(img)]}]}]

    prune_images(messages, keep=3)          # nothing pruned: thinking stays
    assert early.raw[0]["type"] == "thinking" and late.raw[0]["type"] == "thinking"

    prune_images(messages, keep=2)          # the first frame goes, so both later thinking blocks go too
    assert early.raw == [tool_use] and late.raw == [tool_use]

    prune_images(messages, keep=1)          # also prunes the frame after `early`: `late` is after it
    assert late.raw == [tool_use]
