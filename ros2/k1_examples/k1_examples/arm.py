"""
A small blocking client for the K1's ROS 2 interface, used by the example programs.

    with K1Arm() as arm:
        arm.move({"joint1": 0.3}, seconds=2.0)
        arm.gripper(0.0)

It uses the same interfaces as any ROS tool (MoveIt included):
    arm_controller/follow_joint_trajectory  control_msgs/FollowJointTrajectory  (served by k1_web's web_node)
    gripper_controller/gripper_cmd          control_msgs/GripperCommand
    enable                                  std_srvs/SetBool                    (k1_driver)
    enabled                                 std_msgs/Bool
    joint_states                            sensor_msgs/JointState

Every motion goes through web_node, so it gets k1_web's joint limits and speed limit, and STOP on the web page
aborts it.
"""
import threading
import time

from action_msgs.msg import GoalStatus
from builtin_interfaces.msg import Duration
from control_msgs.action import FollowJointTrajectory, GripperCommand
import rclpy
from rclpy.action import ActionClient
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from sensor_msgs.msg import JointState
from std_msgs.msg import Bool
from std_srvs.srv import SetBool
from trajectory_msgs.msg import JointTrajectoryPoint

from unitree_k1_sdk import gripper_finger_target

ARM_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5", "joint6"]
# the SDK's names for the same joints, accepted too
ALIASES = dict(zip(["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_roll", "wrist_flex", "gripper_roll"],
                   ARM_JOINTS))


class ArmError(RuntimeError):
    pass


def duration(seconds: float) -> Duration:
    whole = int(seconds)
    return Duration(sec=whole, nanosec=int((seconds - whole) * 1e9))


def wait(future, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while not future.done():
        if time.monotonic() > deadline:
            raise ArmError(f"timed out waiting for {what}")
        time.sleep(0.01)
    return future.result()


class K1Arm:

    def __init__(self, node_name: str = "k1_example", server_timeout: float = 5.0):
        if not rclpy.ok():
            rclpy.init()
        self.node = Node(node_name)
        self.trajectory_client = ActionClient(self.node, FollowJointTrajectory, "arm_controller/follow_joint_trajectory")
        self.gripper_client = ActionClient(self.node, GripperCommand, "gripper_controller/gripper_cmd")
        self.enable_client = self.node.create_client(SetBool, "enable")
        self._joint_state: JointState | None = None
        self._enabled: bool | None = None
        self.node.create_subscription(JointState, "joint_states", self._on_joint_state, 10)
        self.node.create_subscription(Bool, "enabled", self._on_enabled,
                                      QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))

        self.executor = MultiThreadedExecutor()
        self.executor.add_node(self.node)
        self._spin = threading.Thread(target=self.executor.spin, daemon=True)
        self._spin.start()

        if not self.trajectory_client.wait_for_server(timeout_sec=server_timeout):
            self.close()
            raise ArmError("no trajectory action server; is `ros2 launch k1_web k1_bringup.launch.py` running?")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        # stop spinning before destroying the node, so no callback runs on a destroyed node
        if rclpy.ok():
            rclpy.shutdown()
        self._spin.join(timeout=2.0)
        self.node.destroy_node()

    def _on_joint_state(self, msg: JointState):
        self._joint_state = msg

    def _on_enabled(self, msg: Bool):
        self._enabled = msg.data

    # ---- state ----

    @property
    def enabled(self) -> bool | None:
        """The driver's motor state, or None if it hasn't been received."""
        return self._enabled

    def joint_positions(self, timeout: float = 5.0) -> dict[str, float]:
        """The latest measured positions by URDF joint name."""
        deadline = time.monotonic() + timeout
        while self._joint_state is None:
            if time.monotonic() > deadline:
                raise ArmError("no joint_states received; is the driver running?")
            time.sleep(0.02)
        msg = self._joint_state
        return dict(zip(msg.name, msg.position))

    # ---- commands ----

    def enable(self, value: bool = True):
        """Lock (True) or release (False) the motors. Releasing makes the arm fall: support it first."""
        if not self.enable_client.wait_for_service(timeout_sec=2.0):
            raise ArmError("the enable service is not available; is the driver running?")
        response = wait(self.enable_client.call_async(SetBool.Request(data=value)), 3.0, "enable")
        if not response.success:
            raise ArmError(f"enable failed: {response.message}")

    def move(self, positions, seconds: float = 3.0, timeout: float | None = None) -> FollowJointTrajectory.Result:
        """
        Move to a pose over `seconds`.

        positions: 6 numbers (joint1..joint6), or a {name: radians} dict with URDF or SDK joint names;
        joints left out stay where they are. Raises ArmError if the goal is refused or not reached.
        """
        if isinstance(positions, dict):
            names = [ALIASES.get(name, name) for name in positions]
            values = [float(v) for v in positions.values()]
        else:
            names, values = list(ARM_JOINTS), [float(v) for v in positions]
        goal = FollowJointTrajectory.Goal()
        goal.trajectory.joint_names = names
        goal.trajectory.points = [JointTrajectoryPoint(positions=values, time_from_start=duration(seconds))]
        return self._send(self.trajectory_client, goal, timeout or seconds + 30.0, "trajectory")

    def gripper(self, open_fraction: float, timeout: float = 10.0) -> GripperCommand.Result:
        """1.0 opens fully, 0.0 closes. Sent as the finger position in metres, like any GripperCommand client."""
        if not self.gripper_client.wait_for_server(timeout_sec=2.0):
            raise ArmError("no gripper action server")
        goal = GripperCommand.Goal()
        goal.command.position = gripper_finger_target(min(max(open_fraction, 0.0), 1.0))
        return self._send(self.gripper_client, goal, timeout, "gripper")

    def _send(self, client, goal, timeout: float, what: str):
        handle = wait(client.send_goal_async(goal), 5.0, f"the {what} goal to be accepted")
        if not handle.accepted:
            raise ArmError(f"the {what} goal was rejected")
        try:
            response = wait(handle.get_result_async(), timeout, f"the {what} result")
        except (ArmError, KeyboardInterrupt):
            try:
                wait(handle.cancel_goal_async(), 2.0, f"the {what} goal to cancel")
            except ArmError:
                pass
            raise
        result = response.result
        if response.status != GoalStatus.STATUS_SUCCEEDED:
            text = getattr(result, "error_string", "") or "aborted"
            raise ArmError(f"{what} failed: {text}")
        return result
