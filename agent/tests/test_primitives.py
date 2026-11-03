import json
import os

import pytest

from k1_agent.client import K1Client, StoppedByOperator
from k1_agent.primitives import Primitives
from k1_agent.trace import Trace
from conftest import motors_on


@pytest.fixture
def k1(live_server):
    with K1Client(live_server["url"], live_server["token"]) as client:
        yield client


def make(k1, live_server, **kw):
    trace = Trace(live_server["runs"], "test")
    return Primitives(k1, trace, **kw), trace


def test_interlocks(k1, live_server):
    prims, _ = make(k1, live_server)
    assert prims.move_joints([0.0] * 6)["error"] == "safety_interlock"
    assert prims.enable()["error"] == "safety_interlock"
    prims.allow_motion = True
    assert prims.move_joints([0.0] * 6)["error"] == "not_ready"     # motors still off
    assert prims.stop()["ok"]                                        # always allowed


def test_enable_needs_both_the_flag_and_the_server(k1, live_server):
    prims, _ = make(k1, live_server, allow_enable=True)
    result = prims.enable()
    assert result["error"] == "rejected" and "agent_allow_enable" in result["message"]   # k1_web's own policy
    live_server["sim"].agent_policy.allow_enable = True
    assert prims.enable()["ok"] and k1.state["enabled"]


def test_moves_traces_and_budget(k1, live_server):
    prims, trace = make(k1, live_server, allow_motion=True, max_steps=4)
    motors_on(live_server, k1)

    obs = prims.observe()
    assert obs["ok"] and obs["image"].startswith(b"\xff\xd8") and obs["joints"]["shoulder_pan"] == 0.0

    result = prims.move_joints({"shoulder_pan": 0.3})
    assert result["ok"] and abs(result["joints"]["shoulder_pan"] - 0.3) < 0.05

    result = prims.move_joint_delta("elbow_flex", 0.5)
    assert result["ok"] and result["delta_capped_to"] == 0.2

    result = prims.move_joints([2.0, 0, 0, 0, 0, 0])
    assert result["ok"] and result["clamped"] == {"shoulder_pan": 1.0}

    assert prims.go_home()["ok"]
    assert prims.move_joints([0.0] * 6)["error"] == "step_budget_exhausted"
    assert prims.move_joints("nope")["error"] == "step_budget_exhausted"     # budget checked first

    lines = [json.loads(line) for line in open(trace.path, encoding="utf-8")]
    names = [e["name"] for e in lines]
    assert names[:2] == ["observe", "move_joints"] and "go_pose" in names
    assert all(os.path.exists(os.path.join(trace.dir, e["frame"])) for e in lines if "frame" in e)
    assert "image" not in lines[0]["result"]


def test_human_stop_aborts(k1, live_server):
    prims, _ = make(k1, live_server, allow_motion=True)
    motors_on(live_server, k1)
    with K1Client(live_server["url"], live_server["token"], role="human") as human:
        human.request({"type": "stop"})
        with pytest.raises(StoppedByOperator):
            prims.wait(5)
    with pytest.raises(StoppedByOperator):
        prims.move_joints([0.0] * 6)


def test_own_stop_does_not_abort(k1, live_server):
    prims, _ = make(k1, live_server)
    prims.stop()
    assert prims.wait(0.1)["ok"]
