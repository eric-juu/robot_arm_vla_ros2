"""
Arm control state shared between the ROS node and the web server. No ROS imports, so it can be unit tested.

Clients (the browser, later an AI agent) only ever set a *goal*. A fixed rate `tick()` moves the *commanded*
pose toward the goal by at most `max_velocity * dt` per joint, and the ROS node sends whatever `tick()`
returns to the driver. This keeps every motion smooth and rate limited, whoever asked for it.
"""
import math
import threading
import time

ARM_JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_roll", "wrist_flex", "gripper_roll"]
N_ARM_JOINTS = len(ARM_JOINTS)
JOINT_NAMES = ARM_JOINTS + ["gripper"]
# joint_states may use these names (k1_description's URDF) or JOINT_NAMES; both map to the same index
URDF_JOINT_NAMES = ["joint1", "joint2", "joint3", "joint4", "joint5", "joint6", "joint_gripper_left"]
JOINT_INDEX = {name: i for names in (JOINT_NAMES, URDF_JOINT_NAMES) for i, name in enumerate(names)}


class CommandError(ValueError):
    """A client command that was rejected. The message is shown to the client."""


def validate_positions(positions) -> list[float]:
    if not isinstance(positions, (list, tuple)) or len(positions) != N_ARM_JOINTS:
        raise CommandError(f"expected a list of {N_ARM_JOINTS} joint positions")
    # bool is an int subclass, reject it explicitly
    if not all(isinstance(p, (int, float)) and not isinstance(p, bool) for p in positions):
        raise CommandError("joint positions must be numbers")
    positions = [float(p) for p in positions]
    if not all(math.isfinite(p) for p in positions):
        raise CommandError("joint positions must be finite")
    return positions


def order_joint_state(names, positions) -> list[float] | None:
    """
    Puts joint_states positions in JOINT_NAMES order: the 6 arm joints, then the gripper if present.

    Matches by name (JOINT_NAMES or the URDF names). A message with no names, or none of these names, is taken
    in order. Returns None when an arm joint is missing.
    """
    if not any(name in JOINT_INDEX for name in names):
        return [float(p) for p in positions[:len(JOINT_NAMES)]] if len(positions) >= N_ARM_JOINTS else None
    ordered: list[float | None] = [None] * len(JOINT_NAMES)
    for name, position in zip(names, positions):
        index = JOINT_INDEX.get(name)
        if index is not None:
            ordered[index] = float(position)
    if any(p is None for p in ordered[:N_ARM_JOINTS]):
        return None
    return ordered if ordered[N_ARM_JOINTS] is not None else ordered[:N_ARM_JOINTS]


def clamp(positions: list[float], limits_min: list[float], limits_max: list[float]) -> list[float]:
    return [min(max(p, lo), hi) for p, lo, hi in zip(positions, limits_min, limits_max)]


def step_toward(current: list[float], goal: list[float], max_step: float) -> list[float]:
    """Move each joint toward the goal by at most max_step. Lands exactly on the goal once within reach."""
    out = []
    for c, g in zip(current, goal):
        delta = g - c
        out.append(g if abs(delta) <= max_step else c + math.copysign(max_step, delta))
    return out


class ArmController:

    def __init__(self, limits_min, limits_max, max_velocity: float, state_timeout: float,
                 deadman_timeout: float, clock=time.monotonic):
        if len(limits_min) != N_ARM_JOINTS or len(limits_max) != N_ARM_JOINTS:
            raise ValueError(f"joint limits need {N_ARM_JOINTS} values each")
        if any(lo > hi for lo, hi in zip(limits_min, limits_max)):
            raise ValueError("every joint limit min must be <= its max")
        if max_velocity <= 0.0:
            raise ValueError(f"max_velocity must be positive, got {max_velocity}")

        self.limits_min = [float(v) for v in limits_min]
        self.limits_max = [float(v) for v in limits_max]
        self.max_velocity = max_velocity
        self.state_timeout = state_timeout
        self.deadman_timeout = deadman_timeout
        self.clock = clock

        self._lock = threading.Lock()
        self._measured: list[float] | None = None
        self._measured_time: float | None = None
        self._enabled = False
        self._commanded: list[float] | None = None   # None until seeded from a fresh joint state
        self._goal: list[float] | None = None
        self._gripper: float | None = None           # last gripper value sent
        self._pending_gripper: float | None = None
        self._goal_seq = 0                            # counts goal changes, so a trajectory notices interruptions
        self._clients = 0
        self._last_heartbeat = -math.inf
        self._image: bytes | None = None
        self._image_seq = 0

    # ---- inputs from ROS ----

    def on_joint_state(self, names, positions):
        ordered = order_joint_state(list(names), list(positions))
        if ordered is None:
            return
        with self._lock:
            self._measured = ordered
            self._measured_time = self.clock()

    def on_image(self, jpeg: bytes):
        with self._lock:
            self._image = jpeg
            self._image_seq += 1

    def set_enabled(self, enabled: bool):
        with self._lock:
            self._enabled = enabled
            self._goal_seq += 1
            # while released the arm can be moved by hand, so always reseed from the measured pose
            self._commanded = None
            self._goal = None
            self._pending_gripper = None

    # ---- client connection tracking (deadman) ----

    def client_connected(self):
        with self._lock:
            self._clients += 1
            self._last_heartbeat = self.clock()

    def client_disconnected(self):
        with self._lock:
            self._clients = max(0, self._clients - 1)
            if self._clients == 0 and self._commanded is not None:
                self._goal = list(self._commanded)

    def heartbeat(self):
        with self._lock:
            self._last_heartbeat = self.clock()

    # ---- client commands; raise CommandError when rejected ----

    def _require_ready(self):
        if not self._enabled:
            raise CommandError("motors are disabled")
        if self._commanded is None:
            raise CommandError("waiting for a fresh joint state from the arm")

    def set_goal(self, positions) -> int:
        """Sets the goal, clamped to the limits. Returns the new goal sequence number."""
        positions = validate_positions(positions)
        with self._lock:
            self._require_ready()
            self._goal = clamp(positions, self.limits_min, self.limits_max)
            self._goal_seq += 1
            return self._goal_seq

    def set_gripper(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise CommandError("gripper value must be a finite number")
        with self._lock:
            self._require_ready()
            self._pending_gripper = min(max(float(value), 0.0), 1.0)

    def stop(self):
        with self._lock:
            self._goal_seq += 1
            if self._commanded is not None:
                self._goal = list(self._commanded)

    @property
    def goal_seq(self) -> int:
        """Changes on every goal, STOP and enable/disable, whoever sent it."""
        with self._lock:
            return self._goal_seq

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def not_ready_reason(self) -> str | None:
        """Why a goal would not be followed right now, or None if it would."""
        with self._lock:
            if not self._enabled:
                return "motors are disabled"
            if not self._is_fresh(self.clock()):
                return "no fresh joint state from the arm"
            if self._commanded is None:
                return "waiting for a fresh joint state from the arm"
            return None

    def commanded_arm(self) -> list[float] | None:
        with self._lock:
            return list(self._commanded) if self._commanded is not None else None

    def measured(self) -> list[float] | None:
        """The last measured positions in JOINT_NAMES order (the gripper only if reported), fresh or not."""
        with self._lock:
            return list(self._measured) if self._measured is not None else None

    def measured_arm(self) -> list[float]:
        with self._lock:
            if not self._is_fresh(self.clock()):
                raise CommandError("no fresh joint state from the arm")
            return self._measured[:N_ARM_JOINTS]

    @property
    def gripper(self) -> float | None:
        with self._lock:
            return self._gripper

    # ---- periodic ----

    def _is_fresh(self, now: float) -> bool:
        return self._measured_time is not None and now - self._measured_time <= self.state_timeout

    def tick(self, dt: float):
        """
        Advance the commanded pose by one control period.

        Returns (arm, gripper): the 6 joint positions to send, or None when nothing should be sent,
        and the gripper value to send, or None.
        """
        now = self.clock()
        with self._lock:
            if not self._enabled:
                return None, None
            if not self._is_fresh(now):
                # don't drive the arm blind; hold, and resume only on a new command
                if self._commanded is not None:
                    self._goal = list(self._commanded)
                return None, None
            if self._commanded is None:
                self._commanded = self._measured[:N_ARM_JOINTS]
                self._goal = list(self._commanded)
            if self._clients == 0 or now - self._last_heartbeat > self.deadman_timeout:
                self._goal = list(self._commanded)

            arm = None
            if self._goal != self._commanded:
                self._commanded = step_toward(self._commanded, self._goal, self.max_velocity * dt)
                arm = list(self._commanded)

            gripper = self._pending_gripper
            self._pending_gripper = None
            if gripper is not None:
                self._gripper = gripper
            return arm, gripper

    def snapshot(self) -> dict:
        now = self.clock()
        with self._lock:
            return {
                "names": JOINT_NAMES[:len(self._measured)] if self._measured is not None else [],
                "positions": list(self._measured) if self._measured is not None else None,
                "stale": not self._is_fresh(now),
                "enabled": self._enabled,
                "ready": self._enabled and self._commanded is not None,
                "goal": list(self._goal) if self._goal is not None else None,
                "commanded": list(self._commanded) if self._commanded is not None else None,
                "gripper": self._gripper,
                "limits_min": list(self.limits_min),
                "limits_max": list(self.limits_max),
            }

    def latest_image(self) -> tuple[int, bytes | None]:
        with self._lock:
            return self._image_seq, self._image
