"""
Tests run against the simulated arm (k1_agent.sim.SimServer): the real k1_web FastAPI app, no ROS, on a free local
port, with a thread that ticks the ArmController like the ROS timer does.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

from k1_agent.sim import SimServer  # noqa: E402

TOKEN = "agent-test-token-0123"
TEST_POSES = {"home": {"joints": [0.1, 0.0, 0.0, 0.0, 0.0, 0.0], "gripper": 1.0}}


@pytest.fixture
def live_server(tmp_path):
    sim = SimServer(limits=1.0, max_velocity=2.0, poses=TEST_POSES, token=TOKEN, workdir=str(tmp_path)).start()
    sim.reset([0.0] * 6, enabled=False)     # motors off, as after a real bringup
    yield {"url": sim.url, "token": TOKEN, "controller": sim.controller, "sim": sim, "runs": str(tmp_path / "runs")}
    sim.stop()


def motors_on(live_server, client, joints=None):
    """Enable the simulated arm (as a person would from the page) and wait until `client` has seen it."""
    live_server["sim"].reset(joints or [0.0] * 6, enabled=True)
    assert client.wait_for(lambda s: s.get("ready"), timeout=3.0, after_seq=client.state_seq)
