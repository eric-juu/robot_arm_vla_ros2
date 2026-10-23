import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from k1_web.control import ArmController  # noqa: E402
from k1_web.poses import PoseStore  # noqa: E402
from k1_web.server import WebConfig, create_app  # noqa: E402

TOKEN = "test-token-0123456789"
ADMIN = "admin-password-test"


@pytest.fixture
def setup(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    for name in ["index.html", "login.html", "app.js", "style.css"]:
        (static / name).write_text(name)
    defaults = tmp_path / "poses.yaml"
    defaults.write_text("poses:\n  zero:\n    joints: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]\n    gripper: 1.0\n")

    controller = ArmController([-1.0] * 6, [1.0] * 6, 0.5, state_timeout=60.0, deadman_timeout=60.0)
    poses = PoseStore(str(defaults), str(tmp_path / "user" / "poses.yaml"))

    async def enable_fn(value):
        controller.set_enabled(value)
        return True, "motors enabled" if value else "motors disabled"

    app = create_app(controller, poses, enable_fn, WebConfig(TOKEN, str(static), admin_password=ADMIN))
    return TestClient(app), controller, poses, tmp_path


def login(client):
    assert client.post("/login", json={"token": TOKEN}).status_code == 200


def test_auth_gates_everything_but_login(setup):
    client, controller, _, _ = setup
    assert client.get("/").text == "login.html"
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/../web_node.py").status_code == 404
    assert client.get("/snapshot.jpg").status_code == 401
    assert client.post("/login", json={"token": "wrong"}).status_code == 401
    with pytest.raises(Exception):
        with client.websocket_connect("/ws"):
            pass

    login(client)
    assert client.get("/").text == "index.html"
    assert client.get("/snapshot.jpg").status_code == 503      # authed, but no frame yet
    controller.on_image(b"\xff\xd8jpeg")
    assert client.get("/snapshot.jpg").content == b"\xff\xd8jpeg"


def test_cookie_is_https_only_over_https(setup):
    client, _, _, _ = setup
    assert "secure" not in client.post("/login", json={"token": TOKEN}).headers["set-cookie"].lower()
    https = TestClient(client.app, base_url="https://testserver")
    response = https.post("/login", json={"token": f"  {TOKEN}\n"})     # pasted with whitespace
    assert response.status_code == 200 and "secure" in response.headers["set-cookie"].lower()


def test_bearer_token(setup):
    client, _, _, _ = setup
    with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
        assert ws.receive_json()["type"] == "state"


def test_admin_password(setup):
    client, controller, poses, tmp_path = setup
    with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {ADMIN}"}) as ws:
        assert ws.receive_json()["type"] == "state"
    assert client.post("/login", json={"token": ADMIN}).status_code == 200
    assert client.get("/").text == "index.html"

    no_admin = TestClient(create_app(controller, poses, None, WebConfig(TOKEN, str(tmp_path / "static"))))
    assert no_admin.post("/login", json={"token": ADMIN}).status_code == 401
    assert no_admin.post("/login", json={"token": ""}).status_code == 401


def test_control_flow_over_websocket(setup):
    client, controller, poses, tmp_path = setup
    login(client)
    controller.on_joint_state(["j"] * 7, [0.1] * 7)
    with client.websocket_connect("/ws") as ws:
        state = ws.receive_json()
        assert state["type"] == "state" and state["poses"] == ["zero"] and not state["enabled"]

        ws.send_json({"type": "goal", "positions": [0.0] * 6})
        assert ws.receive_json() == {"type": "error", "message": "motors are disabled"}

        ws.send_json({"type": "enable", "value": True})
        msg = ws.receive_json()
        while msg["type"] == "state":
            msg = ws.receive_json()
        assert msg == {"type": "enable_result", "success": True, "message": "motors enabled"}

        controller.tick(0.02)       # the ROS timer would do this; seeds from the measured pose
        ws.send_json({"type": "pose", "name": "zero"})
        ws.send_json({"type": "save_pose", "name": "here"})
        msg = ws.receive_json()
        while msg["type"] == "state":
            msg = ws.receive_json()
        assert msg == {"type": "info", "message": "saved pose 'here'"}

        ws.send_json({"type": "save_pose", "name": "../evil"})
        ws.send_json({"type": "bogus"})
        ws.send_text("not json")
        errors = []
        while len(errors) < 3:
            msg = ws.receive_json()
            if msg["type"] == "error":
                errors.append(msg["message"])
        assert "pose name" in errors[0] and "unknown message type" in errors[1] and errors[2] == "invalid JSON"

    assert controller.snapshot()["goal"] == controller.snapshot()["commanded"]     # deadman on disconnect
    assert poses.get("here")["joints"] == [0.1] * 6
    assert PoseStore("", str(tmp_path / "user" / "poses.yaml")).names() == ["here"]     # persisted


def receive_until(ws, kind):
    msg = ws.receive_json()
    while msg["type"] != kind:
        msg = ws.receive_json()
    return msg


def test_roles_stop_broadcast_and_notes(setup):
    client, _, _, _ = setup
    login(client)
    with client.websocket_connect("/ws") as human, \
            client.websocket_connect("/ws", headers={"Authorization": f"Bearer {TOKEN}"}) as agent:
        agent.send_json({"type": "hello", "role": "agent", "id": 1})
        assert receive_until(agent, "ack") == {"type": "ack", "id": 1}
        for _ in range(50):     # states queued before the hello still count the agent as human
            if receive_until(human, "state")["clients"] == {"human": 1, "agent": 1}:
                break
        else:
            raise AssertionError("agent never showed up in state.clients")

        agent.send_json({"type": "agent_note", "text": "  looking for the cup  "})
        assert receive_until(human, "agent_note") == {"type": "agent_note", "text": "looking for the cup", "by": "agent"}

        human.send_json({"type": "stop"})
        assert receive_until(agent, "event") == {"type": "event", "name": "stop", "by": "human"}

        agent.send_json({"type": "goal", "positions": [0.0] * 6, "id": "g1"})
        assert receive_until(agent, "error") == {"type": "error", "message": "motors are disabled", "id": "g1"}
        agent.send_json({"type": "hello", "role": "root", "id": 2})
        assert "role" in receive_until(agent, "error")["message"]


def test_prompting_the_agent_service(setup):
    client, _, _, _ = setup
    login(client)
    with client.websocket_connect("/ws") as human, \
            client.websocket_connect("/ws", headers={"Authorization": f"Bearer {TOKEN}"}) as agent:
        human.send_json({"type": "agent_prompt", "text": "wave", "id": 1})
        assert "no agent service" in receive_until(human, "error")["message"]

        agent.send_json({"type": "hello", "role": "agent", "id": 1})
        receive_until(agent, "ack")
        agent.send_json({"type": "agent_status", "status": {"state": "idle", "provider": "claude-code"}, "id": 2})
        receive_until(agent, "ack")
        for _ in range(50):
            if receive_until(human, "state")["agent"] == {"state": "idle", "provider": "claude-code"}:
                break
        else:
            raise AssertionError("agent status never showed up in state")

        human.send_json({"type": "agent_prompt", "text": " wave hello ", "mode": "code", "id": 2})
        assert receive_until(human, "ack") == {"type": "ack", "id": 2}
        assert receive_until(agent, "agent_prompt") == {"type": "agent_prompt", "text": "wave hello", "mode": "code"}
        agent.send_json({"type": "agent_prompt", "text": "loop", "id": 3})
        assert "only for human" in receive_until(agent, "error")["message"]

        agent.send_json({"type": "agent_question", "qid": "q1", "text": "Is the cup there?", "kind": "confirm"})
        assert receive_until(human, "agent_question") == {
            "type": "agent_question", "qid": "q1", "text": "Is the cup there?", "kind": "confirm"}
        human.send_json({"type": "agent_answer", "qid": "q1", "text": "yes"})
        assert receive_until(agent, "agent_answer") == {"type": "agent_answer", "qid": "q1", "text": "yes"}

        agent.send_json({"type": "agent_status", "status": {"state": "running", "task": "wave"}, "id": 4})
        receive_until(agent, "ack")
        human.send_json({"type": "agent_prompt", "text": "again", "id": 5})
        assert "busy" in receive_until(human, "error")["message"]
