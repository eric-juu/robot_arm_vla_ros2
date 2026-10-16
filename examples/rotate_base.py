"""
Rotates the base of the Unitree K1 arm left and right.

Usage:
    python examples/rotate_base.py /dev/ttyACM0
"""
import sys
import time

from unitree_k1_sdk import UnitreeK1Robot, ZERO_POSE


if __name__ == "__main__":
    port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"

    arm = UnitreeK1Robot(port)
    arm.connect()
    arm.enable()

    # start from the zero pose, same as zero.py
    print("going to zero pose...")
    arm.step(ZERO_POSE)
    time.sleep(3)

    # joint 0 is the base (shoulder_pan); angles are in radians (0.5 rad is about 30 degrees)
    pose = ZERO_POSE.copy()

    print("rotating base left...")
    pose[0] = 0.5
    arm.step(pose)
    time.sleep(3)

    print("rotating base right...")
    pose[0] = -0.5
    arm.step(pose)
    time.sleep(3)

    print("back to center...")
    pose[0] = 0.0
    arm.step(pose)
    time.sleep(3)

    # WARNING: the robot will collapse when the motors are disabled
    input("Hold the arm, then press Enter to disable the motors...")
    arm.disable()
