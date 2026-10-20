"""
Runs the real k1_web FastAPI app (no ROS) on a free local port with a simulated arm: a thread
ticks the ArmController like the ROS timer does and reports the commanded pose as the measured one.
"""
import os
import socket
import sys
import threading
import time

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(REPO, "ros2", "k1_web"))
sys.path.insert(0, os.path.join(REPO, "agent"))

pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")

from k1_web.control import ArmController  # noqa: E402
from k1_web.poses import PoseStore  # noqa: E402
from k1_web.server import WebConfig, create_app  # noqa: E402

TOKEN = "agent-test-token-0123"
JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_roll", "wrist_flex", "gripper_roll", "gripper"]
FAKE_JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SimArm:
    """Stands in for the ROS node: ticks the controller and echoes the command as joint_states."""

    def __init__(self, controller: ArmController):
        self.controller = controller
        self.arm = [0.0] * 6
        self.gripper = 1.0
        self.running = True
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def loop(self):
        while self.running:
            arm, gripper = self.controller.tick(0.02)
            if arm is not None:
                self.arm = list(arm)
            if gripper is not None:
                self.gripper = gripper
            self.controller.on_joint_state(JOINTS, self.arm + [self.gripper])
            self.controller.on_image(FAKE_JPEG)
            time.sleep(0.02)


@pytest.fixture
def live_server(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    for name in ["index.html", "login.html", "app.js", "style.css"]:
        (static / name).write_text(name)
    defaults = tmp_path / "poses.yaml"
    defaults.write_text("poses:\n  home:\n    joints: [0.1, 0.0, 0.0, 0.0, 0.0, 0.0]\n    gripper: 1.0\n")

    controller = ArmController([-1.0] * 6, [1.0] * 6, 2.0, state_timeout=1.0, deadman_timeout=1.0)
    poses = PoseStore(str(defaults), str(tmp_path / "user_poses.yaml"))

    async def enable_fn(value):
        controller.set_enabled(value)
        return True, "motors enabled" if value else "motors disabled"

    app = create_app(controller, poses, enable_fn, WebConfig(TOKEN, str(static), state_rate_hz=50.0))
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    sim = SimArm(controller)
    sim.thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)

    yield {"url": f"http://127.0.0.1:{port}", "token": TOKEN, "controller": controller,
           "runs": str(tmp_path / "runs")}

    sim.running = False
    server.should_exit = True
    thread.join(timeout=5)
