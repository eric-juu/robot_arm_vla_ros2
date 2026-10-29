"""
A simulated K1 for evaluations and tests: the real k1_web app (no ROS) on a free local port, with a thread
that ticks the ArmController like the ROS timer does and reports the commanded pose as the measured one.

    with SimServer() as sim:
        client = K1Client(sim.url, sim.token).connect()
        ...
        sim.reset([0.0] * 6)            # put the arm back between trials
        sim.block("elbow_flex", 0.15)   # a "wall": that joint can't go past 0.15 rad (moves end not_reached)

The camera is a fixed grey frame saying "SIMULATED ARM", so evaluations here test planning, tool use and
honesty, not vision.
"""
import base64
import os
import secrets
import socket
import sys
import threading
import time

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
K1_WEB_DIR = os.path.join(os.path.dirname(AGENT_DIR), "ros2", "k1_web")
ARM_JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_roll", "wrist_flex", "gripper_roll"]
SIM_POSES = {"home": {"joints": [0.0] * 6, "gripper": 1.0},
             "ready": {"joints": [0.0, -0.3, 0.5, 0.0, -0.2, 0.0], "gripper": 1.0}}
SIM_FRAME = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZGNywtQFdBRkxO"
    "UlNSMj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09P"
    "T09PT09PT0//wAARCAB4AKADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUF"
    "BAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVW"
    "V1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi"
    "4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAEC"
    "AxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVm"
    "Z2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq"
    "8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDIooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAoooo"
    "AKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooHJwKke3njJEkMiEDcdykYGcZ+maAI6KkNvOJHjMMgdAWddpyoHUkdq"
    "aYpBnMbDaoY8dAcYP05H5igBtFKUdZDGyMHB2lSOQfTFSpaXUjOqW0zMhwwCElT7+lAENFTNZ3SIXa2mVB1YoQBTZra4gAM8EseT"
    "gb0I/nQBHRT1hlYIViciRtqYU/MfQep5FOa1uUJDW8qkLvIKEYX1+lAEVFOaN1jWRkYI+drEcHHXBptABRRRQAUUUUAFFFFABRRR"
    "QBLasUuonDqhVwQzDIHPU1oStEtnOoZEdoiPKSUOoPmRnKnJ64PGT0rKooA6C4uYDdXc6zRlplmiOGHICvj8/wB3j1qjM6eTPKJE"
    "IltYolAYbty7M5HUfcNZtFAGrMqR6zPdvJE0QleVdkyktySvQnvjtTPNha8E6sFElrICGcEhvLZcE8cnAP41m0UAaEyxSadbMNrO"
    "kRBPnqNvzscbTyeD+tSXxVJdSYyxutxIfLCSBs/PndweOMj8ay6KANu1mthLbQFwv2eSJ/MaQbT83zAcf7RPX+Gksrq2jtSFbZGU"
    "ZWR3DNy8e7042+3Y1i0UAaOovGLK2t45EfyJHX5Wz/CmT9C27BrOoooAKKKKACiiigAooooAKKKKACprNFlvYI3GVeRVYeoJqGig"
    "C/HFDLAJUgG/5wsYJO8jb75zhiePSgW6ebKI4fNZSn7rJ4yPm6c8HiqFFAGqtlDi33RYDTRqTuJDK2c89D07UxILaWNMxiLdGHLB"
    "j8v73Yep9OazaKANJreBZkDQMvySMUbK52qSP4ietR3Fsiaesoj2sWTkZwQwJ7/QfT3qjRQAUUUUAFFFFABRRRQAUUUUAFFFFABR"
    "RRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQA"
    "UUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFAH/2Q=="
)


def import_k1_web():
    """k1_web from this repo, unless it is already installed (for example from the ROS workspace)."""
    try:
        import k1_web.server  # noqa: F401
    except ImportError:
        sys.path.insert(0, K1_WEB_DIR)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SimServer:

    def __init__(self, limits=1.0, max_velocity: float = 1.0, poses: dict | None = None, token: str | None = None,
                 workdir: str | None = None):
        import_k1_web()
        import tempfile

        import uvicorn
        import yaml
        from k1_web.control import ArmController
        from k1_web.poses import PoseStore
        from k1_web.server import WebConfig, create_app

        self.token = token or secrets.token_hex(16)
        self._tmp = None if workdir else tempfile.TemporaryDirectory(prefix="k1sim-")
        workdir = workdir or self._tmp.name
        static = os.path.join(workdir, "static")
        os.makedirs(static, exist_ok=True)
        for name in ["index.html", "login.html", "app.js", "style.css"]:
            with open(os.path.join(static, name), "w") as f:
                f.write(name)
        poses_file = os.path.join(workdir, "poses.yaml")
        with open(poses_file, "w") as f:
            yaml.safe_dump({"poses": poses or SIM_POSES}, f)

        low, high = (limits if isinstance(limits, tuple) else ([-limits] * 6, [limits] * 6))
        self.controller = ArmController(low, high, max_velocity, state_timeout=1.0, deadman_timeout=1.0)
        self.poses = PoseStore(poses_file, os.path.join(workdir, "user_poses.yaml"))
        self.arm = [0.0] * 6
        self.gripper = 1.0
        self.blocked: dict[int, float] = {}
        self._lock = threading.Lock()

        async def enable_fn(value):
            self.controller.set_enabled(value)
            return True, "motors enabled" if value else "motors disabled"

        app = create_app(self.controller, self.poses, enable_fn, WebConfig(self.token, static, state_rate_hz=50.0))
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self._running = False

    def start(self) -> "SimServer":
        self._running = True
        threading.Thread(target=self._server.run, daemon=True).start()
        threading.Thread(target=self._loop, daemon=True).start()
        deadline = time.monotonic() + 10
        while not self._server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self._server.started:
            raise RuntimeError("the simulated k1_web did not start")
        self.reset(self.arm)
        return self

    def stop(self):
        self._running = False
        self._server.should_exit = True
        if self._tmp is not None:
            self._tmp.cleanup()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def _loop(self):
        while self._running:
            arm, gripper = self.controller.tick(0.02)
            with self._lock:
                if arm is not None:
                    self.arm = [min(p, self.blocked[i]) if i in self.blocked else p for i, p in enumerate(arm)]
                if gripper is not None:
                    self.gripper = gripper
                measured = self.arm + [self.gripper]
            self.controller.on_joint_state(ARM_JOINTS + ["gripper"], measured)
            self.controller.on_image(SIM_FRAME)
            time.sleep(0.02)

    def reset(self, joints=None, gripper: float = 1.0, enabled: bool = True):
        """Put the arm in a pose (motors enabled, nothing blocked) and let the controller reseed from it."""
        with self._lock:
            self.arm = list(joints if joints is not None else [0.0] * 6)
            self.gripper = gripper
            self.blocked = {}
        time.sleep(0.1)                                 # a fresh joint state at the new pose
        self.controller.set_enabled(False)
        self.controller.set_enabled(enabled)
        deadline = time.monotonic() + 2
        while enabled and self.controller.not_ready_reason() is not None and time.monotonic() < deadline:
            time.sleep(0.02)

    def block(self, joint: str, max_value: float):
        """A wall: `joint` stops at `max_value`, so moves beyond it never finish."""
        with self._lock:
            self.blocked[ARM_JOINTS.index(joint)] = max_value
