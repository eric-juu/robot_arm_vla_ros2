"""
A simulated K1 for evaluations and tests: the real k1_web app (no ROS) on a free local port, with a thread
that ticks the ArmController like the ROS timer does and reports the commanded pose as the measured one.

    with SimServer(scene="bench") as sim:
        client = K1Client(sim.url, sim.token).connect()
        ...
        sim.reset([0.0] * 6)            # put the arm back between trials (and the scene, if it has one)
        sim.block("elbow_flex", 0.15)   # a "wall": that joint can't go past 0.15 rad (moves end not_reached)

With a scene (k1_agent.scene: blocks, a dev board with a button, LEDs and a display, notes, a hand...) the wrist
camera is rendered from the arm's pose, fresh for every snapshot, and the scene pushes back: the arm stops short
of the table and solid objects, buttons count presses, and a closed gripper holds a block. Without a scene the
camera is a fixed grey "SIMULATED ARM" frame, which is enough for tests of planning, tool use and honesty.

Faults for the evaluation's robustness tasks: measurement noise, a calibration offset on one joint, a joint whose
zero has shifted, a gripper that stalls, joint states that go stale for a while, a camera that drops out, and a
block that slips out of the gripper (the methods at the end of SimServer).
"""
import base64
import os
import random
import secrets
import socket
import sys
import threading
import time

from k1_agent.primitives import ARM_JOINTS

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
K1_WEB_DIR = os.path.join(os.path.dirname(AGENT_DIR), "ros2", "k1_web")
SIM_POSES = {"zero": {"joints": [0.0] * 6},
             "home": {"joints": [0.0] * 6, "gripper": 1.0},
             "ready": {"joints": [0.0, -0.3, 0.5, 0.0, -0.2, 0.0], "gripper": 1.0},
             "overview": {"joints": [0.0, 0.2, -0.4, 0.0, -1.3, 0.0]},      # looking straight down at the work area
             # taught for the "bench" scene's reset button: fingers closed and pointing down, 5 cm above / pressing
             "bench_above": {"joints": [0.1, -0.55, 0.25, 0.0, -1.4, 0.0], "gripper": 0.0},
             "bench_press": {"joints": [0.1, -0.7, 0.25, 0.0, -1.4, 0.0], "gripper": 0.0}}
# The simulated arm's limits: shoulder_pan as in k1_web.yaml's placeholder (some tasks rely on 1.0), the others
# wide enough to look straight down at the table, inside the URDF's ranges.
SIM_LIMITS = ([-1.0, -1.0, -1.4, -1.5, -1.6, -1.5], [1.0, 1.0, 1.4, 1.5, 1.6, 1.5])
FINGER_TRAVEL_M = 0.034
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

    def __init__(self, limits=SIM_LIMITS, max_velocity: float = 1.0, poses: dict | None = None,
                 token: str | None = None, workdir: str | None = None, agent_policy: dict | None = None,
                 scene=None):
        """`limits`: a number (+- for every joint) or (mins, maxs). `agent_policy`: k1_web's AgentPolicy fields
        (allow_enable, free_motion, limits_min/max, max_velocity), the defaults being the real server's; the
        policy object stays reachable as `self.agent_policy`. `scene`: a k1_agent.scene spec, or None."""
        import_k1_web()
        import tempfile

        import uvicorn
        import yaml
        from k1_web.control import ArmController
        from k1_web.policy import AgentPolicy
        from k1_web.poses import PoseStore
        from k1_web.server import WebConfig, create_app

        sim = self

        class SimController(ArmController):
            """Renders the wrist camera when someone asks for a frame, so a snapshot always shows the current pose."""

            def latest_image(self):
                return sim.frame()

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
        self.controller = SimController(low, high, max_velocity, state_timeout=1.0, deadman_timeout=1.0)
        self.poses = PoseStore(poses_file, os.path.join(workdir, "user_poses.yaml"))
        self.arm = [0.0] * 6
        self.gripper = 1.0
        self.blocked: dict[int, float] = {}
        self._lock = threading.Lock()
        self._render_lock = threading.Lock()
        self.scene_spec = scene
        self.scene = None
        self.contacts = 0                       # poses refused because the gripper would have hit something
        self.touches = 0                        # separate times it ran into something (contacts counts every tick)
        self._touching = False
        self._frame = (0, SIM_FRAME)
        self._frame_key = None
        self._clear_faults()

        async def enable_fn(value):
            self.controller.set_enabled(value)
            return True, "motors enabled" if value else "motors disabled"

        self.agent_policy = AgentPolicy(**(agent_policy or {}))
        app = create_app(self.controller, self.poses, enable_fn,
                         WebConfig(self.token, static, state_rate_hz=50.0, agent_policy=self.agent_policy))
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

    # ---- the arm ----

    def _loop(self):
        while self._running:
            arm, gripper = self.controller.tick(0.02)
            with self._lock:
                if gripper is not None:
                    self.gripper = gripper
                if arm is not None:
                    target = [min(p, self.blocked[i]) if i in self.blocked else p for i, p in enumerate(arm)]
                    target = [p + self._zero_shifts.get(i, 0.0) for i, p in enumerate(target)]
                    if self.scene is not None and self.scene.collides(target, self.gripper):
                        self.contacts += 1      # the gripper would hit the table or an object: it stops short
                        self.touches += not self._touching
                        self._touching = True
                    else:
                        self.arm = target
                        self._touching = False
                if self.scene is not None and (arm is not None or gripper is not None):
                    self.scene.step(self.arm, self.gripper)
                measured = self._measured()
                stale = time.monotonic() < self._stale_until
            if not stale:
                self.controller.on_joint_state(ARM_JOINTS + ["gripper"], measured)
            time.sleep(0.02)

    def _measured(self) -> list[float]:
        arm = list(self.arm)
        for i, offset in self._offsets.items():
            arm[i] += offset
        for i, shift in self._zero_shifts.items():
            arm[i] -= shift
        if self._noise:
            arm = [p + self._rng.gauss(0.0, self._noise) for p in arm]
        stop = self.scene.gripper_stop(self.gripper) if self.scene is not None else self.gripper
        if self._gripper_stall is not None:
            stop = max(stop, self._gripper_stall)
        # like the real driver: the finger position in metres, 0 open .. -0.034 closed
        return arm + [-FINGER_TRAVEL_M * (1.0 - min(max(stop, 0.0), 1.0))]

    def reset(self, joints=None, gripper: float = 1.0, enabled: bool = True, scene=None):
        """Put the arm in a pose (motors enabled, nothing blocked, no faults), rebuild the scene (`scene`, or the
        one given at construction) and let the controller reseed from the pose."""
        from k1_agent.scene import Scene

        spec = scene if scene is not None else self.scene_spec
        with self._lock:
            self.arm = list(joints if joints is not None else [0.0] * 6)
            self.gripper = gripper
            self.blocked = {}
            self.contacts = self.touches = 0
            self._touching = False
            self.scene = Scene.from_spec(spec) if spec is not None else None
            self._frame_key = None
            self._clear_faults()
        time.sleep(0.1)                                 # a fresh joint state at the new pose
        self.controller.set_enabled(False)
        self.controller.set_enabled(enabled)
        deadline = time.monotonic() + 2
        while enabled and self.controller.not_ready_reason() is not None and time.monotonic() < deadline:
            time.sleep(0.02)

    # ---- the camera ----

    def frame(self) -> tuple[int, bytes | None]:
        """The current wrist-camera frame (rendered if the arm or the scene changed), or None during a dropout."""
        if not self._camera_on:
            return self._frame[0], None
        if self.scene is None:
            return self._frame
        with self._lock:
            key = (tuple(round(p, 4) for p in self.arm), round(self.gripper, 3), self.scene.version)
            arm, gripper, scene = list(self.arm), self.gripper, self.scene
        with self._render_lock:
            if key != self._frame_key:
                self._frame = (self._frame[0] + 1, scene.render(arm, gripper))
                self._frame_key = key
            return self._frame

    def view(self, name: str) -> dict:
        """Where an object is in the current camera image (see Scene.in_view)."""
        with self._lock:
            return self.scene.in_view(name, list(self.arm))

    # ---- faults ----

    def _clear_faults(self):
        self._offsets: dict[int, float] = {}
        self._zero_shifts: dict[int, float] = {}
        self._noise = 0.0
        self._rng = random.Random(0)
        self._gripper_stall: float | None = None
        self._stale_until = 0.0
        self._camera_on = True

    def block(self, joint: str, max_value: float):
        """A wall: `joint` stops at `max_value`, so moves beyond it never finish."""
        with self._lock:
            self.blocked[ARM_JOINTS.index(joint)] = max_value

    def set_noise(self, sigma: float, seed: int = 0):
        """Gaussian noise (rad) on every measured joint."""
        with self._lock:
            self._noise, self._rng = float(sigma), random.Random(seed)

    def set_offset(self, joint: str, rad: float):
        """A calibration error: `joint` always reads `rad` away from where it is, so moves end not_reached."""
        with self._lock:
            self._offsets[ARM_JOINTS.index(joint)] = float(rad)

    def set_zero_shift(self, joint: str, rad: float):
        """A joint that has lost its zero: it sits `rad` away from where it is commanded and reads as if it were
        right, so every move ends ok while the arm is really somewhere else (a sensor `offset` is the opposite)."""
        with self._lock:
            index = ARM_JOINTS.index(joint)
            self._zero_shifts[index] = float(rad)
            self.arm[index] += float(rad)

    def stall_gripper(self, value: float):
        """The fingers never close past `value` (1 open .. 0 closed), as if something were stuck between them."""
        with self._lock:
            self._gripper_stall = float(value)

    def go_stale(self, seconds: float):
        """Stop reporting joint states for a while (a cable glitch): k1_web holds the arm and reports `stale`."""
        with self._lock:
            self._stale_until = time.monotonic() + float(seconds)

    def camera(self, on: bool):
        """Turn the camera off (snapshots return 503) or back on."""
        self._camera_on = bool(on)

    def drop_held(self):
        """The block in the gripper slips out and falls to the table."""
        with self._lock:
            if self.scene is not None:
                self.scene.drop()
