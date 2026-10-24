"""
Demo: turns the base left and right, bends the elbow, closes and opens the gripper, and returns to where it
started. The ROS version of the SDK's examples/demo.py, so it keeps k1_web's limits, speed limit and STOP.

    ros2 run k1_examples demo            # enable the motors on the web page first
    ros2 run k1_examples demo --enable   # or let the demo enable them

The motors stay enabled at the end; disable them from the web page once someone is holding the arm.
"""
import argparse
import sys

from k1_examples.arm import ArmError, K1Arm


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--enable", action="store_true", help="enable the motors first")
    parser.add_argument("--angle", type=float, default=0.3, help="how far to move each joint, radians (default 0.3)")
    args = parser.parse_args()

    try:
        with K1Arm("k1_demo") as arm:
            if args.enable:
                arm.enable(True)
            elif arm.enabled is False:
                sys.exit("the motors are disabled: enable them on the web page, or pass --enable")

            start = arm.joint_positions()
            home = {name: start[name] for name in ("joint1", "joint2", "joint3", "joint4", "joint5", "joint6")}
            print("start (rad):", {k: round(v, 3) for k, v in home.items()})

            print("turning the base...")
            arm.move({"joint1": home["joint1"] + args.angle}, seconds=2.0)
            arm.move({"joint1": home["joint1"] - args.angle}, seconds=3.0)
            arm.move({"joint1": home["joint1"]}, seconds=2.0)

            print("bending the elbow...")
            arm.move({"joint3": home["joint3"] + args.angle}, seconds=2.0)
            arm.move({"joint3": home["joint3"]}, seconds=2.0)

            print("closing and opening the gripper...")
            arm.gripper(0.0)
            arm.gripper(1.0)

            arm.move(home, seconds=2.0)
            print("done; the motors are still enabled")
    except ArmError as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        sys.exit("interrupted; the arm holds where it is")


if __name__ == "__main__":
    main()
