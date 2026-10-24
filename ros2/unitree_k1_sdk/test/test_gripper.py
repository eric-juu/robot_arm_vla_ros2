import math

import pytest

from unitree_k1_sdk import gripper_finger_position, gripper_finger_target, gripper_open_fraction
from unitree_k1_sdk.k1 import GRIPPER_CLOSE_VALUE, UnitreeK1Robot


def test_open_is_zero_metres_and_closed_is_negative():
    assert gripper_finger_target(1.0) == 0.0
    assert gripper_finger_target(0.0) == pytest.approx(-0.0361, abs=1e-4)


def test_fraction_round_trips():
    for value in (0.0, 0.25, 0.5, 1.0):
        assert gripper_open_fraction(gripper_finger_target(value)) == pytest.approx(value)


def test_fraction_is_clamped():
    assert gripper_open_fraction(0.01) == 1.0
    assert gripper_open_fraction(-1.0) == 0.0


def test_measured_value_uses_the_same_scale_as_the_command():
    # a servo reporting the fully closed raw value, converted like the other joints (raw / 1800 * pi)
    rad = GRIPPER_CLOSE_VALUE / 1800.0 * math.pi
    assert gripper_finger_position(rad) == pytest.approx(gripper_finger_target(0.0))


def test_set_gripper_rad_uses_the_scaled_value():
    arm = UnitreeK1Robot("unused")
    sent = []
    arm.set_gripper = sent.append
    arm.set_gripper_rad(2.0944)       # fully closed
    arm.set_gripper_rad(0.0)          # fully open
    assert sent == [pytest.approx(0.0), 1.0]
