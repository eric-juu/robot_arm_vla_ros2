"""
Demo script for the Unitree K1 arm.

Connects to the arm, reads its current pose, moves a few joints smoothly,
opens and closes the gripper, returns to where it started, and then
releases the motors.

Usage:
    python examples/demo.py /dev/ttyACM0

SAFETY: clear the space around the arm, and be ready to hold it up at the
end, because it goes limp when the motors are disabled.
"""
import sys
import time

import numpy as np

from unitree_k1_sdk import UnitreeK1Robot


# Joint order used everywhere in the SDK (7 values, all in radians):
#   0 shoulder_pan, 1 shoulder_lift, 2 elbow_flex, 3 wrist_roll,
#   4 wrist_flex,   5 gripper_roll,  6 gripper
CONTROL_HZ = 50  # how many position commands we send per second


def move_smoothly(arm: UnitreeK1Robot, start: np.ndarray, goal: np.ndarray, duration: float):
    """
    Move from `start` to `goal` over `duration` seconds.

    The SDK's step() makes the arm jump straight to the target (its move time
    is hardcoded to 0), so to get smooth motion we send many small
    intermediate targets ourselves.
    """
    n_steps = max(1, int(duration * CONTROL_HZ))
    for i in range(1, n_steps + 1):
        alpha = i / n_steps                       # goes from 0 to 1
        target = start + alpha * (goal - start)   # linear blend between poses
        arm.step(target)
        time.sleep(1.0 / CONTROL_HZ)


if __name__ == "__main__":
    # The serial port can be given on the command line; default to /dev/ttyACM0.
    port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"

    # 1. Open the serial connection. This also starts a background thread
    #    that keeps arm.joint_position_measured up to date.
    arm = UnitreeK1Robot(port)
    arm.connect()

    # 2. Turn the motors on so the arm holds its position.
    arm.enable()
    time.sleep(0.5)

    # 3. Read where the arm is right now. We start every motion from here, so
    #    the arm doesn't jump on the first command.
    start_pose = arm.joint_position_measured.copy()
    start_pose[6] = 0.0   # gripper: 0 rad means fully open in step()
    print("Starting pose (rad):", np.round(start_pose, 3))

    # 4. Rotate the base left, then right. Small angles (0.3 rad is about 17
    #    degrees) keep this demo safe.
    print("Rotating base...")
    left = start_pose.copy()
    left[0] += 0.3
    right = start_pose.copy()
    right[0] -= 0.3
    move_smoothly(arm, start_pose, left, duration=2.0)
    move_smoothly(arm, left, right, duration=3.0)
    move_smoothly(arm, right, start_pose, duration=2.0)

    # 5. Bend the elbow a little and come back.
    print("Bending elbow...")
    bent = start_pose.copy()
    bent[2] += 0.3
    move_smoothly(arm, start_pose, bent, duration=2.0)
    move_smoothly(arm, bent, start_pose, duration=2.0)

    # 6. Close and open the gripper. set_gripper() takes 1.0 = open and
    #    0.0 = closed. It's called directly here (not through step(), which
    #    would also reset the gripper), stepping slowly so the motion is smooth.
    print("Closing gripper...")
    for value in np.linspace(1.0, 0.0, 40):
        arm.set_gripper(value)
        time.sleep(0.025)
    time.sleep(0.5)
    print("Opening gripper...")
    for value in np.linspace(0.0, 1.0, 40):
        arm.set_gripper(value)
        time.sleep(0.025)

    # 7. Print the measured pose to compare with where we started.
    print("Final pose (rad):   ", np.round(arm.joint_position_measured, 3))

    # 8. Release the motors. The arm will drop, so wait until someone is
    #    holding it.
    input("Hold the arm, then press Enter to disable the motors...")
    arm.disable()
    time.sleep(0.1)   # give the disable command time to go out

    # 9. Close the serial connection.
    arm.disconnect()
    print("Done.")
