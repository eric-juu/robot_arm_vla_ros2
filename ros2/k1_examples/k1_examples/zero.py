"""
Move the arm to the zero pose (all joints 0 rad: upper arm up, forearm pointing forward).

    ros2 run k1_examples zero
    ros2 run k1_examples zero --time 6
"""
import argparse
import sys

from k1_examples.arm import ArmError, K1Arm


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--time", type=float, default=4.0, help="seconds to take (default 4)")
    args = parser.parse_args()
    try:
        with K1Arm("k1_zero") as arm:
            arm.move([0.0] * 6, seconds=args.time)
            print("at the zero pose")
    except ArmError as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        sys.exit("canceled; the arm holds where it is")


if __name__ == "__main__":
    main()
