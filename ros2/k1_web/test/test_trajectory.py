import math

import pytest

from k1_web.control import CommandError
from k1_web.trajectory import Trajectory, outside_limits, tolerances, within

START = [0.0] * 6


def test_samples_between_points_and_holds_unnamed_joints():
    t = Trajectory(["joint1", "elbow_flex"], [(1.0, [1.0, 0.5]), (3.0, [0.0, 0.5])], [0.0, 0.2, 0.0, 0, 0, 0])
    assert t.duration == 3.0
    assert t.sample(0.0) == [0.0, 0.2, 0.0, 0, 0, 0]
    assert t.sample(0.5) == pytest.approx([0.5, 0.2, 0.25, 0, 0, 0])
    assert t.sample(2.0) == pytest.approx([0.5, 0.2, 0.5, 0, 0, 0])
    assert t.sample(10.0) == t.final == [0.0, 0.2, 0.5, 0, 0, 0]


def test_point_at_time_zero_is_the_goal_immediately():
    t = Trajectory(["joint1"], [(0.0, [0.4])], START)
    assert t.duration == 0.0
    assert t.sample(0.0) == [0.4, 0, 0, 0, 0, 0]


@pytest.mark.parametrize("names, points, message", [
    (["joint1"], [], "no points"),
    (["joint9"], [(1.0, [0.0])], "unknown joint"),
    (["joint_gripper_left"], [(1.0, [0.0])], "gripper action"),
    (["joint1", "shoulder_pan"], [(1.0, [0.0, 0.0])], "repeat"),
    (["joint1"], [(1.0, [0.0, 0.1])], "positions for 1 joints"),
    (["joint1"], [(1.0, [math.nan])], "finite"),
    (["joint1"], [(2.0, [0.1]), (1.0, [0.2])], "must not decrease"),
])
def test_rejects_bad_goals(names, points, message):
    with pytest.raises(CommandError, match=message):
        Trajectory(names, points, START)


def test_outside_limits_names_the_joints():
    lo, hi = [-1.0] * 6, [1.0] * 6
    assert outside_limits([[0.0] * 6, [1.5, 0, 0, 0, 0, -2.0]], lo, hi) == ["shoulder_pan", "gripper_roll"]
    assert outside_limits([[1.0] * 6], lo, hi) == []


def test_tolerances_follow_control_msgs_conventions():
    tols = tolerances([("joint1", 0.1), ("joint2", 0.0), ("joint3", -1.0)], default=0.05)
    assert tols == [0.1, 0.05, None, 0.05, 0.05, 0.05]
    assert within([0.09, 0.0, 5.0, 0, 0, 0], [0.0] * 6, tols)
    assert not within([0.11, 0.0, 0.0, 0, 0, 0], [0.0] * 6, tols)
