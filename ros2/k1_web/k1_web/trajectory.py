"""
Joint trajectories for the FollowJointTrajectory action in web_node. No ROS imports, so it can be unit tested.

A trajectory is sampled at the control rate and each sample becomes the ArmController's goal, so it still
gets the joint limits, the speed limit and STOP. Joints the trajectory doesn't name hold where they started.
"""
import bisect
import math

from k1_web.control import ARM_JOINTS, JOINT_INDEX, N_ARM_JOINTS, CommandError


def arm_index(name: str) -> int:
    index = JOINT_INDEX.get(name)
    if index is None or index >= N_ARM_JOINTS:
        if index == N_ARM_JOINTS:
            raise CommandError(f"{name!r} is the gripper; use the gripper action instead")
        raise CommandError(f"unknown joint {name!r}; use joint1..joint6 or {ARM_JOINTS}")
    return index


class Trajectory:

    def __init__(self, joint_names, points, start):
        """
        joint_names: names of the joints in each point (URDF or JOINT_NAMES names, any subset of the arm)
        points: (time_from_start in seconds, positions) pairs
        start: the 6 arm positions the trajectory starts from
        """
        if not points:
            raise CommandError("the trajectory has no points")
        indices = [arm_index(name) for name in joint_names]
        if len(set(indices)) != len(indices):
            raise CommandError(f"joint names repeat: {list(joint_names)}")

        self.times = [0.0]
        self.waypoints = [[float(p) for p in start]]
        for t, positions in points:
            if len(positions) != len(indices):
                raise CommandError(f"a point has {len(positions)} positions for {len(indices)} joints")
            if not all(math.isfinite(p) for p in positions) or not math.isfinite(t):
                raise CommandError("trajectory values must be finite")
            if t < self.times[-1]:
                raise CommandError("time_from_start must not decrease")
            waypoint = list(self.waypoints[-1])
            for index, position in zip(indices, positions):
                waypoint[index] = float(position)
            self.times.append(float(t))
            self.waypoints.append(waypoint)

    @property
    def duration(self) -> float:
        return self.times[-1]

    @property
    def final(self) -> list[float]:
        return list(self.waypoints[-1])

    def sample(self, t: float) -> list[float]:
        """Linear interpolation between waypoints; the last waypoint once t is past the end."""
        if t >= self.duration:
            return self.final
        i = bisect.bisect_right(self.times, t)    # times[i-1] <= t < times[i]
        t0, t1 = self.times[i - 1], self.times[i]
        a, b = self.waypoints[i - 1], self.waypoints[i]
        alpha = (t - t0) / (t1 - t0) if t1 > t0 else 1.0
        return [p + alpha * (q - p) for p, q in zip(a, b)]


def outside_limits(waypoints, limits_min, limits_max, slack: float = 1e-6) -> list[str]:
    """Names of the arm joints that any waypoint puts outside the limits."""
    bad = set()
    for waypoint in waypoints:
        for i, (p, lo, hi) in enumerate(zip(waypoint, limits_min, limits_max)):
            if p < lo - slack or p > hi + slack:
                bad.add(i)
    return [ARM_JOINTS[i] for i in sorted(bad)]


def tolerances(named, default: float) -> list[float | None]:
    """
    Per joint position tolerances from control_msgs JointTolerance-style (name, position) pairs.

    As in control_msgs: 0 means the default, a negative value means don't check that joint.
    """
    result: list[float | None] = [default] * N_ARM_JOINTS
    for name, value in named:
        index = arm_index(name)
        if value < 0:
            result[index] = None
        elif value > 0:
            result[index] = value
    return result


def within(actual, target, tols) -> bool:
    return all(tol is None or abs(a - t) <= tol for a, t, tol in zip(actual, target, tols))
