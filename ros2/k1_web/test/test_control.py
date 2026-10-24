import math

import pytest

from k1_web.control import ArmController, CommandError, clamp, order_joint_state, step_toward, validate_positions


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


def test_joint_states_are_ordered_by_name():
    urdf = ["joint_gripper_left", "joint6", "joint5", "joint4", "joint3", "joint2", "joint1"]
    assert order_joint_state(urdf, [7, 6, 5, 4, 3, 2, 1]) == [1, 2, 3, 4, 5, 6, 7]
    sdk = ["gripper_roll", "wrist_flex", "wrist_roll", "elbow_flex", "shoulder_lift", "shoulder_pan"]
    assert order_joint_state(sdk, [6, 5, 4, 3, 2, 1]) == [1, 2, 3, 4, 5, 6]     # no gripper reported
    assert order_joint_state(["joint1", "joint2"], [1, 2]) is None                # arm joints missing
    assert order_joint_state([], [1, 2, 3, 4, 5, 6, 7]) == [1, 2, 3, 4, 5, 6, 7]  # unnamed: in order


def test_snapshot_uses_friendly_names_whatever_joint_states_used():
    c = make_controller(FakeClock())
    c.on_joint_state(["joint1", "joint2", "joint3", "joint4", "joint5", "joint6", "joint_gripper_left"],
                     [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, -0.01])
    snap = c.snapshot()
    assert snap["names"] == ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_roll", "wrist_flex",
                             "gripper_roll", "gripper"]
    assert snap["positions"] == [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, -0.01]


def test_goal_seq_changes_on_every_goal_stop_and_enable():
    c, clock = ready_controller()
    seq = c.set_goal([0.1] * 6)
    assert c.goal_seq == seq
    c.stop()
    assert c.goal_seq == seq + 1
    assert c.set_goal([0.2] * 6) == seq + 2
    c.set_enabled(False)
    assert c.goal_seq == seq + 3


def test_not_ready_reason():
    clock = FakeClock()
    c = make_controller(clock)
    assert c.not_ready_reason() == "motors are disabled"
    c.set_enabled(True)
    assert c.not_ready_reason() == "no fresh joint state from the arm"
    c.on_joint_state([], [0.0] * 7)
    assert c.not_ready_reason() == "waiting for a fresh joint state from the arm"
    c.client_connected()
    c.tick(0.02)
    assert c.not_ready_reason() is None
    assert c.commanded_arm() == [0.0] * 6
    clock.t += 1.0
    assert c.not_ready_reason() == "no fresh joint state from the arm"
