"""
The K1 primitive API: the small, fixed set of actions an AI agent (or a program it writes) can use.

Every call returns a dict with "ok". Failures are typed results ({"ok": False, "error": "<code>",
"message": ...}) rather than exceptions, so the model can read them and react, the way ASPIRE
treats planner refusals. The one exception is StoppedByOperator: when a human presses STOP the
task is over, and it propagates.

All motion still goes through k1_web, which clamps to joint limits, limits velocity and holds the
arm if the connection drops. On top of that:
    allow_motion  motion primitives refuse unless the run was started with --allow-motion
    allow_enable  enable() refuses unless started with --allow-enable
    max_steps     budget of motion calls per run
    max_step_rad  cap on move_joint_delta
"""
import functools
import math

from k1_agent.client import K1Client, K1Error, StoppedByOperator
from k1_agent.trace import Trace

ARM_JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_roll", "wrist_flex", "gripper_roll"]
MAX_WAIT_SEC = 10.0


def fail(error: str, message: str, **extra) -> dict:
    return {"ok": False, "error": error, "message": message, **extra}


def primitive(frame: bool = False, motion: bool = False):
    """Trace a primitive call; `frame` saves a camera frame after it, `motion` applies the interlocks."""
    def wrap(fn):
        @functools.wraps(fn)
        def call(self, *args, **kwargs):
            named = dict(zip(fn.__code__.co_varnames[1:], args), **kwargs)
            before = self._arm_positions()
            try:
                result = self._motion_guard() if motion else None
                if result is None:
                    result = fn(self, *args, **kwargs)
            except StoppedByOperator:
                self._record(fn.__name__, named, fail("stopped_by_operator", "STOP pressed"), before, None)
                raise
            except K1Error as e:
                result = fail("connection", str(e))
            image = result.pop("image", None) if isinstance(result, dict) else None
            if frame and image is None:
                image = self._snapshot()
            self.last_frame = image if frame else None
            self._record(fn.__name__, named, result, before, self._arm_positions(), image)
            if image is not None and fn.__name__ == "observe":
                result["image"] = image
            return result
        call.is_primitive = True
        return call
    return wrap


class Primitives:

    def __init__(self, client: K1Client, trace: Trace | None = None, allow_motion: bool = False,
                 allow_enable: bool = False, max_steps: int = 60, max_step_rad: float = 0.2,
                 tolerance: float = 0.05, move_timeout: float = 10.0):
        self.client = client
        self.trace = trace
        self.allow_motion = allow_motion
        self.allow_enable = allow_enable
        self.max_steps = max_steps
        self.max_step_rad = max_step_rad
        self.tolerance = tolerance
        self.move_timeout = move_timeout
        self.steps_used = 0
        self._acked_seq = -1
        self.last_frame: bytes | None = None     # camera frame taken after the latest primitive call

    # ---- helpers ----

    def _record(self, name, args, result, before, after, frame=None):
        if self.trace is not None:
            self.trace.record(name, args, result, before, after, frame)

    def _arm_positions(self) -> list | None:
        try:
            positions = self.client.state.get("positions")
        except K1Error:
            return None
        return positions[:len(ARM_JOINTS)] if positions else None

    def _snapshot(self) -> bytes | None:
        try:
            return self.client.snapshot()
        except K1Error:
            return None

    def _joint_names(self, state: dict) -> list[str]:
        names = state.get("names") or []
        return names[:len(ARM_JOINTS)] if len(names) >= len(ARM_JOINTS) else ARM_JOINTS

    def _motion_guard(self) -> dict | None:
        self.client.check_stopped()
        if not self.allow_motion:
            return fail("safety_interlock", "motion is disabled for this run; restart it with --allow-motion")
        if self.steps_used >= self.max_steps:
            return fail("step_budget_exhausted", f"used all {self.max_steps} motion steps for this run")
        state = self.client.state
        if not state.get("ready"):
            return fail("not_ready", "motors are disabled or the arm state hasn't arrived; a human must enable them")
        if state.get("stale"):
            return fail("stale_state", "the arm stopped reporting its state")
        self.steps_used += 1
        return None

    def _send(self, msg: dict) -> dict | None:
        reply = self.client.request(msg)
        self._acked_seq = self.client.state_seq     # states after this one reflect the command
        if reply["type"] == "error":
            return fail("rejected", reply["message"])
        if reply["type"] == "enable_result" and not reply["success"]:
            return fail("rejected", reply["message"])
        return None

    def _wait_reached(self, timeout: float | None) -> dict:
        """Wait for the server to finish its smoothed move and the measured pose to settle on the goal."""
        timeout = min(timeout or self.move_timeout, 30.0)

        def reached(s):
            goal, commanded, measured = s.get("goal"), s.get("commanded"), s.get("positions")
            if not (goal and commanded and measured):
                return False
            return (max(abs(g - c) for g, c in zip(goal, commanded)) < 1e-6
                    and max(abs(g - m) for g, m in zip(goal, measured)) <= self.tolerance)

        state = self.client.wait_for(reached, timeout, after_seq=self._acked_seq) or self.client.state
        names = self._joint_names(state)
        goal = state.get("goal") or []
        measured = (state.get("positions") or [])[:len(goal)]
        residuals = {n: round(g - m, 4) for n, g, m in zip(names, goal, measured)}
        ok = bool(residuals) and max(abs(r) for r in residuals.values()) <= self.tolerance
        result = {"ok": ok, "joints": {n: round(m, 4) for n, m in zip(names, measured)}, "residuals": residuals}
        if not ok:
            result.update(error="not_reached",
                          message=f"the arm did not reach the goal within {timeout:.0f} s (blocked or holding a load?)")
        return result

    def _move(self, target: list[float], timeout: float | None) -> dict:
        state = self.client.state
        lo, hi = state["limits_min"], state["limits_max"]
        clamped = [min(max(v, a), b) for v, a, b in zip(target, lo, hi)]
        error = self._send({"type": "goal", "positions": target})
        if error:
            return error
        result = self._wait_reached(timeout)
        if clamped != target:
            names = self._joint_names(state)
            result["clamped"] = {n: round(c, 4) for n, t, c in zip(names, target, clamped) if t != c}
        return result

    def _base_pose(self) -> list[float]:
        state = self.client.state
        return list(state.get("commanded") or state["positions"][:len(ARM_JOINTS)])

    # ---- primitives ----

    @primitive(frame=True)
    def observe(self) -> dict:
        """The arm state and a fresh wrist-camera frame (the frame is returned under "image")."""
        state = self.client.state
        names = self._joint_names(state)
        positions = state.get("positions") or []
        return {
            "ok": True,
            "enabled": state.get("enabled"),
            "ready": state.get("ready"),
            "stale": state.get("stale"),
            "joints": {n: round(p, 4) for n, p in zip(names, positions)},
            "gripper_command": state.get("gripper"),
            "gripper_measured": round(positions[len(names)], 4) if len(positions) > len(names) else None,
            "limits": {n: [a, b] for n, a, b in zip(names, state["limits_min"], state["limits_max"])},
            "poses": state.get("poses", []),
            "steps_left": self.max_steps - self.steps_used,
            "image": self._snapshot(),
        }

    @primitive(frame=True, motion=True)
    def move_joints(self, positions, timeout: float | None = None) -> dict:
        """Move to absolute joint angles (rad). `positions` is 6 numbers or {joint: angle} for some joints."""
        names = self._joint_names(self.client.state)
        target = self._base_pose()
        if isinstance(positions, dict):
            unknown = [n for n in positions if n not in names]
            if unknown:
                return fail("bad_arguments", f"unknown joints {unknown}, use {names}")
            for name, value in positions.items():
                target[names.index(name)] = value
        elif isinstance(positions, (list, tuple)) and len(positions) == len(names):
            target = list(positions)
        else:
            return fail("bad_arguments", f"positions must be {len(names)} numbers or a {{joint: angle}} dict")
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in target):
            return fail("bad_arguments", "positions must be finite numbers")
        return self._move([float(v) for v in target], timeout)

    @primitive(frame=True, motion=True)
    def move_joint_delta(self, joint, delta: float, timeout: float | None = None) -> dict:
        """Move one joint (name or index 0-5) by `delta` rad, capped at max_step_rad."""
        names = self._joint_names(self.client.state)
        if isinstance(joint, int) and 0 <= joint < len(names):
            joint = names[joint]
        if joint not in names:
            return fail("bad_arguments", f"unknown joint {joint!r}, use {names}")
        if not isinstance(delta, (int, float)) or not math.isfinite(delta):
            return fail("bad_arguments", "delta must be a finite number")
        capped = max(-self.max_step_rad, min(self.max_step_rad, float(delta)))
        target = self._base_pose()
        target[names.index(joint)] += capped
        result = self._move(target, timeout)
        if capped != delta:
            result["delta_capped_to"] = capped
        return result

    @primitive(frame=True, motion=True)
    def go_pose(self, name: str, timeout: float | None = None) -> dict:
        """Move to a saved pose (see observe()["poses"])."""
        if name not in self.client.state.get("poses", []):
            return fail("unknown_pose", f"no pose {name!r}; saved poses: {self.client.state.get('poses', [])}")
        error = self._send({"type": "pose", "name": name})
        return error or self._wait_reached(timeout)

    def go_home(self, timeout: float | None = None) -> dict:
        """Move to the 'home' pose."""
        if "home" not in self.client.state.get("poses", []):
            self._record("go_home", {}, fail("unknown_pose", "no 'home' pose"), self._arm_positions(), None)
            return fail("unknown_pose", "no 'home' pose saved yet; a human can save one from the web page")
        return self.go_pose("home", timeout)

    @primitive(frame=True, motion=True)
    def set_gripper(self, value: float, settle: float = 1.0) -> dict:
        """Gripper opening: 1.0 fully open, 0.0 closed. Waits `settle` seconds for it to move."""
        if not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
            return fail("bad_arguments", "value must be between 0.0 (closed) and 1.0 (open)")
        error = self._send({"type": "gripper", "value": float(value)})
        if error:
            return error
        self.client.sleep(min(max(settle, 0.0), 5.0))
        positions = self.client.state.get("positions") or []
        return {"ok": True, "gripper_command": float(value),
                "gripper_measured": round(positions[6], 4) if len(positions) > 6 else None}

    @primitive()
    def stop(self) -> dict:
        """Hold the current position. Always allowed."""
        return self._send({"type": "stop"}) or {"ok": True}

    @primitive()
    def enable(self, value: bool = True) -> dict:
        """Lock (True) or release (False) the motors. Needs --allow-enable."""
        if not self.allow_enable:
            return fail("safety_interlock", "enabling motors is disabled for this run; a human must do it")
        error = self._send({"type": "enable", "value": bool(value)})
        if error:
            return error
        if value and self.client.wait_for(lambda s: s.get("ready"), timeout=3.0) is None:
            return fail("not_ready", "motors enabled but the arm state didn't arrive")
        return {"ok": True, "enabled": bool(value)}

    @primitive()
    def wait(self, seconds: float) -> dict:
        """Pause, up to 10 s."""
        if not isinstance(seconds, (int, float)) or seconds < 0:
            return fail("bad_arguments", "seconds must be a non-negative number")
        self.client.sleep(min(float(seconds), MAX_WAIT_SEC))
        return {"ok": True}

    @primitive()
    def list_poses(self) -> dict:
        return {"ok": True, "poses": self.client.state.get("poses", [])}

    @primitive()
    def save_pose(self, name: str) -> dict:
        """Save the current measured pose under `name` (letters, digits, _ or -)."""
        return self._send({"type": "save_pose", "name": name}) or {"ok": True, "saved": name}

    def note(self, text: str) -> dict:
        """Show a line in the web page's agent log (not traced as a primitive)."""
        try:
            self.client.note(str(text)[:500])
        except K1Error as e:
            return fail("connection", str(e))
        return {"ok": True}


PROGRAM_API = ["observe", "move_joints", "move_joint_delta", "go_pose", "go_home", "set_gripper",
               "stop", "wait", "list_poses", "note"]
