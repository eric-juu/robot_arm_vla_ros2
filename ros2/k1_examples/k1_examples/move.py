"""
Move the arm to a pose through the trajectory action.

    ros2 run k1_examples move 0.3 0 0 0 0 0            # all 6 joints (joint1..joint6), radians
    ros2 run k1_examples move joint1=0.3 joint5=-0.2   # only some joints; the others stay put
    ros2 run k1_examples move shoulder_pan=0.3 --time 5
"""
import argparse
import sys

from k1_examples.arm import ArmError, K1Arm


def parse_positions(items):
    if all("=" in item for item in items):
        return {name: float(value) for name, value in (item.split("=", 1) for item in items)}
    if len(items) == 6 and not any("=" in item for item in items):
        return [float(item) for item in items]
    raise ValueError("give 6 numbers, or name=value pairs")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("positions", nargs="+", help="6 numbers, or name=value pairs")
    parser.add_argument("--time", type=float, default=3.0, help="seconds to take (default 3)")
    args = parser.parse_args()
    try:
        positions = parse_positions(args.positions)
    except ValueError as e:
        parser.error(str(e))

    try:
        with K1Arm("k1_move") as arm:
            arm.move(positions, seconds=args.time)
            print("reached:", {k: round(v, 3) for k, v in arm.joint_positions().items()})
    except ArmError as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        sys.exit("canceled; the arm holds where it is")


if __name__ == "__main__":
    main()
