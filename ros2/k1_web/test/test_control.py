import math

import pytest

from k1_web.control import ArmController, CommandError, clamp, step_toward, validate_positions


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make_controller(clock):
    return ArmController([-1.0] * 6, [1.0] * 6, max_velocity=0.5, state_timeout=0.5,
                         deadman_timeout=1.0, clock=clock)


def ready_controller():
    """Enabled, seeded from a fresh joint state at all zeros, with one client connected."""
    clock = FakeClock()
    c = make_controller(clock)
    c.on_joint_state(["j"] * 7, [0.0] * 7)
    c.set_enabled(True)
    c.client_connected()
    assert c.tick(0.02) == (None, None)     # seeds commanded, sends nothing
    return c, clock


def test_step_toward_rate_limits_and_lands_on_goal():
    assert step_toward([0.0, 0.0], [1.0, -0.005], 0.01) == [0.01, -0.005]
    assert step_toward([0.995], [1.0], 0.01) == [1.0]


def test_clamp():
    assert clamp([-2.0, 0.5, 2.0], [-1.0] * 3, [1.0] * 3) == [-1.0, 0.5, 1.0]


@pytest.mark.parametrize("bad", [None, [0.0] * 5, [0.0] * 7, [0.0] * 5 + [math.nan],
                                 [0.0] * 5 + [math.inf], [0.0] * 5 + ["1"], [0.0] * 5 + [True]])
def test_validate_rejects(bad):
    with pytest.raises(CommandError):
        validate_positions(bad)


def test_commands_refused_until_enabled_and_seeded():
    clock = FakeClock()
    c = make_controller(clock)
    with pytest.raises(CommandError, match="disabled"):
        c.set_goal([0.0] * 6)
    c.set_enabled(True)
    with pytest.raises(CommandError, match="fresh joint state"):
        c.set_goal([0.0] * 6)
    c.on_joint_state(["j"] * 7, [0.2] * 7)
    c.client_connected()
    c.tick(0.02)
    c.set_goal([0.2] * 6)       # accepted now


def test_seeds_from_measured_pose_without_jumping():
    clock = FakeClock()
    c = make_controller(clock)
    c.on_joint_state(["j"] * 7, [0.3, -0.2, 0.1, 0.0, 0.0, 0.0, 0.5])
    c.set_enabled(True)
    c.client_connected()
    assert c.tick(0.02) == (None, None)
    assert c.snapshot()["commanded"] == [0.3, -0.2, 0.1, 0.0, 0.0, 0.0]


def test_goal_is_clamped_and_approached_at_max_velocity():
    c, clock = ready_controller()
    c.set_goal([5.0, 0.0, 0.0, 0.0, 0.0, -5.0])
    assert c.snapshot()["goal"] == [1.0, 0.0, 0.0, 0.0, 0.0, -1.0]
    arm, _ = c.tick(0.02)
    assert arm == pytest.approx([0.01, 0.0, 0.0, 0.0, 0.0, -0.01])
    for _ in range(200):
        c.heartbeat()
        c.on_joint_state(["j"] * 7, [0.0] * 7)
        c.tick(0.02)
    assert c.snapshot()["commanded"] == [1.0, 0.0, 0.0, 0.0, 0.0, -1.0]
    assert c.tick(0.02) == (None, None)     # nothing is sent once the goal is reached


def test_stop_holds_current_command():
    c, clock = ready_controller()
    c.set_goal([1.0] * 6)
    c.tick(0.02)
    c.stop()
    assert c.tick(0.02) == (None, None)
    assert c.snapshot()["goal"] == c.snapshot()["commanded"]


def test_deadman_on_silence_and_on_disconnect():
    c, clock = ready_controller()
    c.set_goal([1.0] * 6)
    clock.t += 1.5      # no heartbeat for longer than deadman_timeout...
    c.on_joint_state(["j"] * 7, [0.0] * 7)
    assert c.tick(0.02) == (None, None)     # ...so the arm holds

    c.heartbeat()
    c.set_goal([1.0] * 6)
    assert c.tick(0.02)[0] is not None
    c.client_disconnected()
    assert c.tick(0.02) == (None, None)


def test_stale_state_stops_driving():
    c, clock = ready_controller()
    c.set_goal([1.0] * 6)
    clock.t += 0.6
    c.heartbeat()
    assert c.tick(0.02) == (None, None)
    c.on_joint_state(["j"] * 7, [0.0] * 7)
    assert c.tick(0.02) == (None, None)     # the old goal was dropped, it needs a new command


def test_disable_resets_and_gripper_is_clamped():
    c, clock = ready_controller()
    c.set_gripper(3.0)
    assert c.tick(0.02) == (None, 1.0)
    assert c.gripper == 1.0
    c.set_enabled(False)
    assert c.snapshot()["commanded"] is None
    with pytest.raises(CommandError):
        c.set_gripper(0.5)
