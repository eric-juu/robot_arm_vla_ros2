import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from k1_web.control import ArmController, CommandError  # noqa: E402
from k1_web.policy import AgentPolicy  # noqa: E402
from k1_web.poses import PoseStore  # noqa: E402
from k1_web.server import WebConfig, create_app  # noqa: E402

TOKEN = "test-token-0123456789"
AGENT_TOKEN = "agent-token-0123456789"


def test_policy_rules():
    default = AgentPolicy()
    default.check("goal")
    with pytest.raises(CommandError, match="agent_allow_enable"):
        default.check("enable")
    operate = AgentPolicy(free_motion=False)
    operate.check("pose")
    operate.check("gripper")
    for kind in ("goal", "save_pose"):
        with pytest.raises(CommandError, match="agent_free_motion"):
            operate.check(kind)
    with pytest.raises(CommandError, match="agent_allow_motion"):
        AgentPolicy(allow_motion=False).check("gripper")

    station = AgentPolicy(limits_min=[-0.5, -2.0, 0, 0, 0, 0], limits_max=[0.5, 2.0, 0, 0, 0, 0])
    low, high = station.limits([-1.0] * 6, [1.0] * 6)
    assert low[:2] == [-0.5, -1.0] and high[:2] == [0.5, 1.0]       # never wider than the arm's own limits
    assert station.clamp_goal([0.9, 0.9, 0, 0, 0, 0], [-1.0] * 6, [1.0] * 6)[:2] == [0.5, 0.9]
    with pytest.raises(CommandError, match="outside the agent's joint limits"):
        station.check_pose([0.9, 0, 0, 0, 0, 0], [-1.0] * 6, [1.0] * 6)

    # values as they come from ROS parameters: [] and 0.0 mean "the arm's own"
    assert AgentPolicy(limits_min=[], limits_max=[], max_velocity=0.0).summary()["max_velocity"] is None
    with pytest.raises(ValueError):
        AgentPolicy(limits_min=[0.0] * 6)


def test_goal_speed_cap():
    controller = ArmController([-1.0] * 6, [1.0] * 6, 1.0, state_timeout=60.0, deadman_timeout=60.0)
    controller.on_joint_state(["j"] * 6, [0.0] * 6)
    controller.set_enabled(True)
    controller.client_connected()
    controller.tick(0.1)
    controller.set_goal([1.0, 0, 0, 0, 0, 0], max_velocity=0.25)
    arm, _ = controller.tick(0.1)
    assert arm[0] == pytest.approx(0.025)       # 0.25 rad/s, not the arm's 1.0
    controller.set_goal([1.0, 0, 0, 0, 0, 0])
    arm, _ = controller.tick(0.1)
    assert arm[0] == pytest.approx(0.125)


@pytest.fixture
def server(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    for name in ["index.html", "login.html", "app.js", "style.css"]:
        (static / name).write_text(name)
    defaults = tmp_path / "poses.yaml"
    defaults.write_text("poses:\n  zero:\n    joints: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]\n"
                        "  far:\n    joints: [0.9, 0.0, 0.0, 0.0, 0.0, 0.0]\n")
    controller = ArmController([-1.0] * 6, [1.0] * 6, 0.5, state_timeout=60.0, deadman_timeout=60.0)
    poses = PoseStore(str(defaults), "")

    async def enable_fn(value):
        controller.set_enabled(value)
        return True, "ok"

    def make(policy):
        config = WebConfig(TOKEN, str(static), agent_token=AGENT_TOKEN, agent_policy=policy)
        controller.on_joint_state(["j"] * 6, [0.0] * 6)
        controller.set_enabled(True)
        controller.tick(0.02)
        return TestClient(create_app(controller, poses, enable_fn, config)), controller
    return make


def reply(ws, msg):
    ws.send_json({**msg, "id": "r"})
    while True:
        got = ws.receive_json()
        if got.get("id") == "r":
            return got


def test_agent_token_is_always_an_agent(server):
    client, _ = server(AgentPolicy())
    assert client.post("/login", json={"token": AGENT_TOKEN}).status_code == 401    # not a person's login
    assert client.get("/snapshot.jpg", headers={"Authorization": f"Bearer {AGENT_TOKEN}"}).status_code == 503
    with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {AGENT_TOKEN}"}) as ws:
        assert "only connect as an agent" in reply(ws, {"type": "hello", "role": "human"})["message"]
        assert "only for human" in reply(ws, {"type": "agent_answer", "qid": "1", "text": "yes"})["message"]
        assert "agent_allow_enable" in reply(ws, {"type": "enable", "value": False})["message"]
        assert reply(ws, {"type": "stop"})["type"] == "ack"                       # STOP is always allowed
        state = ws.receive_json()
        assert state["agent_policy"]["allow_enable"] is False and state["clients"]["agent"] == 1


def test_operate_policy_allows_only_saved_poses(server):
    client, controller = server(AgentPolicy(free_motion=False, limits_min=[-0.5] + [-1.0] * 5,
                                            limits_max=[0.5] + [1.0] * 5, max_velocity=0.1))
    with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {AGENT_TOKEN}"}) as agent:
        assert "agent_free_motion" in reply(agent, {"type": "goal", "positions": [0.1] + [0.0] * 5})["message"]
        assert "agent_free_motion" in reply(agent, {"type": "save_pose", "name": "x"})["message"]
        assert "outside the agent's joint limits" in reply(agent, {"type": "pose", "name": "far"})["message"]
        assert reply(agent, {"type": "pose", "name": "zero"})["type"] == "ack"
        state = agent.receive_json()
        assert state["limits_max"][0] == 0.5            # the agent is told the bounds it is held to

    with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {TOKEN}"}) as person:
        assert reply(person, {"type": "pose", "name": "far"})["type"] == "ack"     # people aren't limited
        assert person.receive_json()["limits_max"][0] == 1.0


def test_teach_policy_clamps_and_slows_agent_goals(server):
    client, controller = server(AgentPolicy(limits_min=[-0.5] + [-1.0] * 5, limits_max=[0.5] + [1.0] * 5,
                                            max_velocity=0.1))
    with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
        reply(ws, {"type": "hello", "role": "agent"})     # the main token, saying it's an agent: same rules
        assert reply(ws, {"type": "goal", "positions": [0.9] + [0.0] * 5})["type"] == "ack"
        assert controller.snapshot()["goal"][0] == 0.5
        arm, _ = controller.tick(1.0)
        assert arm[0] == pytest.approx(0.1)
