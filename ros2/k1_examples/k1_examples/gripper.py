"""
Open or close the gripper through the gripper action.

    ros2 run k1_examples gripper open
    ros2 run k1_examples gripper close
    ros2 run k1_examples gripper 0.5      # 1.0 open ... 0.0 closed
"""
import argparse
import sys

from k1_examples.arm import ArmError, K1Arm


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("value", help="open, close, or a number from 0.0 (closed) to 1.0 (open)")
    args = parser.parse_args()
    value = {"open": 1.0, "close": 0.0}.get(args.value.lower())
    if value is None:
        try:
            value = float(args.value)
        except ValueError:
            parser.error("value must be open, close or a number")

    try:
        with K1Arm("k1_gripper") as arm:
            result = arm.gripper(value)
            state = "reached" if result.reached_goal else "stopped early (holding something?)"
            print(f"gripper {state}, finger position {result.position * 1000:.1f} mm")
    except ArmError as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        sys.exit("canceled; the arm holds where it is")


if __name__ == "__main__":
    main()
