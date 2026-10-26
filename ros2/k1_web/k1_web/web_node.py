"""
ROS 2 node that serves the K1 web interface.

Bridges the web server (k1_web.server) to the k1_driver node, and is the one place every motion goes
through: the page, the agent and ROS clients all get the same joint limits, speed limit and STOP.

    Subscribes:
        joint_states            sensor_msgs/JointState
        enabled                 std_msgs/Bool               the driver's motor state
        <image_topic>           sensor_msgs/CompressedImage (JPEG), default /image_raw/compressed
    Publishes:
        joint_position_command  std_msgs/Float64MultiArray  smoothed, rate limited, clamped to joint limits
        gripper_command         std_msgs/Float64
    Calls:
        enable                  std_srvs/SetBool
    Actions (the names ros2_control would use, so MoveIt and other tools can drive the arm):
        arm_controller/follow_joint_trajectory  control_msgs/FollowJointTrajectory
        gripper_controller/gripper_cmd          control_msgs/GripperCommand   position in metres per finger,
                                                                              0 open, -0.034 closed (the URDF's)

The auth token is read from the K1_WEB_TOKEN environment variable. An optional second password,
K1_WEB_ADMIN_PASSWORD, is accepted the same way.
"""
import asyncio
import math
import os
import sys
import threading
import time

from ament_index_python.packages import get_package_share_directory
from control_msgs.action import FollowJointTrajectory, GripperCommand
import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data
from rclpy.signals import SignalHandlerOptions
from sensor_msgs.msg import CompressedImage, JointState
from std_msgs.msg import Bool, Float64, Float64MultiArray
from std_srvs.srv import SetBool
import uvicorn

from k1_web.control import N_ARM_JOINTS, URDF_JOINT_NAMES, ArmController, CommandError
from k1_web.image import rotate_jpeg_180
from k1_web.poses import PoseStore
from k1_web.server import WebConfig, create_app
from k1_web.trajectory import Trajectory, outside_limits, tolerances, within
from unitree_k1_sdk import gripper_finger_target, gripper_open_fraction

ENABLE_TIMEOUT_SEC = 3.0
FEEDBACK_PERIOD_SEC = 0.1
LATCHED = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
ERR = FollowJointTrajectory.Result


def seconds(duration) -> float:
    return duration.sec + duration.nanosec * 1e-9


class K1WebNode(Node):

    def __init__(self):
        super().__init__("k1_web")
        share = get_package_share_directory("k1_web")

        self.host = self.declare_parameter("host", "127.0.0.1").value
        self.port = self.declare_parameter("port", 8080).value
        self.control_rate_hz = self.declare_parameter("control_rate_hz", 50.0).value
        self.state_rate_hz = self.declare_parameter("state_rate_hz", 20.0).value
        max_joint_velocity = self.declare_parameter("max_joint_velocity", 0.5).value
        limits_min = self.declare_parameter("joint_limits_min", [-1.0] * N_ARM_JOINTS).value
        limits_max = self.declare_parameter("joint_limits_max", [1.0] * N_ARM_JOINTS).value
        state_timeout_sec = self.declare_parameter("state_timeout_sec", 0.5).value
        deadman_timeout_sec = self.declare_parameter("deadman_timeout_sec", 1.0).value
        self.video_max_fps = self.declare_parameter("video_max_fps", 15.0).value
        image_topic = self.declare_parameter("image_topic", "/image_raw/compressed").value
        self.rotate_camera = self.declare_parameter("camera_rotate_180", True).value
        poses_file = self.declare_parameter("poses_file", "").value or os.path.join(share, "config", "poses.yaml")
        user_poses_file = self.declare_parameter("user_poses_file", "~/.k1_web/poses.yaml").value
        self.secure_cookie = self.declare_parameter("secure_cookie", False).value
        self.goal_tolerance = self.declare_parameter("goal_tolerance_rad", 0.05).value
        self.settle_timeout = self.declare_parameter("settle_timeout_sec", 3.0).value
        self.ready_timeout = self.declare_parameter("ready_timeout_sec", 1.0).value
        self.gripper_settle = self.declare_parameter("gripper_settle_sec", 1.0).value
        self.gripper_tolerance = self.declare_parameter("gripper_tolerance_m", 0.004).value
        self.max_joint_velocity = max_joint_velocity
        self.static_dir = os.path.join(share, "static")

        if self.control_rate_hz <= 0.0 or self.state_rate_hz <= 0.0 or self.video_max_fps <= 0.0:
            raise ValueError("control_rate_hz, state_rate_hz and video_max_fps must be positive")

        self.controller = ArmController(limits_min, limits_max, max_joint_velocity,
                                        state_timeout_sec, deadman_timeout_sec)
        self.poses = PoseStore(poses_file, user_poses_file)
        self._warned_image_format = False
        self._trajectory_lock = threading.Lock()
        self._trajectory_gen = 0

        self.arm_pub = self.create_publisher(Float64MultiArray, "joint_position_command", 10)
        self.gripper_pub = self.create_publisher(Float64, "gripper_command", 10)
        self.create_subscription(JointState, "joint_states", self.on_joint_state, 10)
        self.create_subscription(Bool, "enabled", self.on_enabled, LATCHED)
        self.create_subscription(CompressedImage, image_topic, self.on_image, qos_profile_sensor_data)
        # reentrant so the service response is processed while other callbacks run
        self.enable_client = self.create_client(SetBool, "enable", callback_group=ReentrantCallbackGroup())
        self.create_timer(1.0 / self.control_rate_hz, self.on_control_timer)

        # reentrant: a goal runs for seconds and must not block the control timer or other goals.
        # Every goal is accepted and checked in execute, so the reason for a refusal reaches the client.
        actions = ReentrantCallbackGroup()
        ActionServer(self, FollowJointTrajectory, "arm_controller/follow_joint_trajectory", self.execute_trajectory,
                     goal_callback=lambda goal: GoalResponse.ACCEPT,
                     cancel_callback=lambda goal_handle: CancelResponse.ACCEPT, callback_group=actions)
        ActionServer(self, GripperCommand, "gripper_controller/gripper_cmd", self.execute_gripper,
                     goal_callback=lambda goal: GoalResponse.ACCEPT,
                     cancel_callback=lambda goal_handle: CancelResponse.ACCEPT, callback_group=actions)

        self.get_logger().info(
            f"limits min {limits_min} max {limits_max} rad, max velocity {max_joint_velocity} rad/s, "
            f"poses {self.poses.names()}")

    def on_joint_state(self, msg: JointState):
        self.controller.on_joint_state(msg.name, msg.position)

    def on_enabled(self, msg: Bool):
        if msg.data != self.controller.enabled:
            self.controller.set_enabled(msg.data)
            self.get_logger().info(f"driver reports motors {'enabled' if msg.data else 'disabled'}")

    def on_image(self, msg: CompressedImage):
        if "jpeg" not in msg.format.lower():
            if not self._warned_image_format:
                self.get_logger().warning(f"ignoring camera frames in format {msg.format!r}, need JPEG")
                self._warned_image_format = True
            return
        jpeg = bytes(msg.data)
        if self.rotate_camera:
            try:
                jpeg = rotate_jpeg_180(jpeg)
            except Exception as e:      # a corrupt frame: skip it rather than show it the wrong way up
                self.get_logger().warning(f"could not rotate a camera frame: {e}", throttle_duration_sec=5.0)
                return
        self.controller.on_image(jpeg)

    def on_control_timer(self):
        arm, gripper = self.controller.tick(1.0 / self.control_rate_hz)
        if arm is not None:
            self.arm_pub.publish(Float64MultiArray(data=arm))
        if gripper is not None:
            self.gripper_pub.publish(Float64(data=gripper))

    async def set_enabled(self, value: bool) -> tuple[bool, str]:
        """Call the driver's enable service. Runs on the web server's event loop."""
        if not self.enable_client.service_is_ready():
            return False, "enable service is not available, is k1_driver running?"
        future = self.enable_client.call_async(SetBool.Request(data=value))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ENABLE_TIMEOUT_SEC
        while not future.done():
            if loop.time() > deadline:
                future.cancel()
                return False, "enable service timed out"
            await asyncio.sleep(0.02)
        response = future.result()
        if response.success and self.controller.enabled != value:
            self.controller.set_enabled(value)
        self.get_logger().info(f"enable({value}) from web: {response.message}")
        return response.success, response.message

    # ---- actions ----

    def wait_until_ready(self) -> str | None:
        """Gives a just-enabled arm a moment to report its pose. Returns why it isn't ready, or None."""
        deadline = time.monotonic() + self.ready_timeout
        while (reason := self.controller.not_ready_reason()) is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        return reason

    def execute_trajectory(self, goal_handle):
        result = FollowJointTrajectory.Result()

        def finish(code, text, outcome="abort"):
            result.error_code, result.error_string = code, text
            {"abort": goal_handle.abort, "succeed": goal_handle.succeed, "cancel": goal_handle.canceled}[outcome]()
            # rclpy wants one severity per call site
            if outcome == "succeed":
                self.get_logger().info(f"trajectory succeeded: {text}")
            else:
                self.get_logger().warning(f"trajectory {outcome}: {text}")
            return result

        with self._trajectory_lock:
            self._trajectory_gen += 1
            gen = self._trajectory_gen

        goal = goal_handle.request
        reason = self.wait_until_ready()
        start = self.controller.commanded_arm()
        if reason is not None or start is None:
            return finish(ERR.INVALID_GOAL, reason or "the arm is not ready")
        try:
            trajectory = Trajectory(goal.trajectory.joint_names,
                                    [(seconds(p.time_from_start), list(p.positions)) for p in goal.trajectory.points],
                                    start)
            tols = tolerances([(t.name, t.position) for t in goal.goal_tolerance], self.goal_tolerance)
        except CommandError as e:
            return finish(ERR.INVALID_JOINTS if "joint" in str(e) else ERR.INVALID_GOAL, str(e))
        bad = outside_limits(trajectory.waypoints[1:], self.controller.limits_min, self.controller.limits_max)
        if bad:
            return finish(ERR.INVALID_GOAL, f"outside k1_web's joint limits: {bad}")

        feedback = FollowJointTrajectory.Feedback()
        feedback.joint_names = URDF_JOINT_NAMES[:N_ARM_JOINTS]
        period = 1.0 / self.control_rate_hz
        self.controller.client_connected()    # counts for the deadman while the goal runs
        try:
            began = time.monotonic()
            seq = None
            sent = None
            last_feedback = 0.0
            deadline = None
            while True:
                if goal_handle.is_cancel_requested:
                    self.controller.stop()
                    return finish(ERR.SUCCESSFUL, "canceled", "cancel")
                if gen != self._trajectory_gen:
                    return finish(ERR.PATH_TOLERANCE_VIOLATED, "replaced by a newer trajectory")
                if seq is not None and self.controller.goal_seq != seq:
                    return finish(ERR.PATH_TOLERANCE_VIOLATED,
                                  "interrupted by STOP, a disable or another client's goal")
                if (reason := self.controller.not_ready_reason()) is not None:
                    return finish(ERR.PATH_TOLERANCE_VIOLATED, reason)

                now = time.monotonic()
                t = now - began
                desired = trajectory.sample(t)
                if desired != sent:     # past the end this is the final point, sent once
                    try:
                        seq = self.controller.set_goal(desired)
                    except CommandError as e:
                        return finish(ERR.PATH_TOLERANCE_VIOLATED, str(e))
                    sent = desired
                self.controller.heartbeat()

                measured = (self.controller.measured() or desired)[:N_ARM_JOINTS]
                if now - last_feedback >= FEEDBACK_PERIOD_SEC:
                    last_feedback = now
                    feedback.header.stamp = self.get_clock().now().to_msg()
                    feedback.desired.positions = desired
                    feedback.actual.positions = measured
                    feedback.error.positions = [d - m for d, m in zip(desired, measured)]
                    goal_handle.publish_feedback(feedback)

                if t >= trajectory.duration:
                    if deadline is None:
                        # the speed limit may make the arm lag a fast trajectory; give it time to catch up
                        commanded = self.controller.commanded_arm() or desired
                        travel = max(abs(f - c) for f, c in zip(trajectory.final, commanded))
                        deadline = (now + travel / self.max_joint_velocity + self.settle_timeout
                                    + seconds(goal.goal_time_tolerance))
                    if self.controller.commanded_arm() == trajectory.final and within(measured, trajectory.final, tols):
                        return finish(ERR.SUCCESSFUL, "goal reached", "succeed")
                    if now > deadline:
                        errors = {name: round(m - f, 3) for name, m, f in
                                  zip(feedback.joint_names, measured, trajectory.final)}
                        return finish(ERR.GOAL_TOLERANCE_VIOLATED, f"goal not reached, errors (rad): {errors}")
                time.sleep(period)
        finally:
            self.controller.client_disconnected()

    def execute_gripper(self, goal_handle):
        result = GripperCommand.Result()
        target = goal_handle.request.command.position
        reason = self.wait_until_ready()
        if reason is None and not math.isfinite(target):
            reason = "gripper position must be finite"
        value = gripper_open_fraction(target) if reason is None else None
        if value is not None:
            try:
                self.controller.set_gripper(value)
            except CommandError as e:
                reason = str(e)
        if reason is not None:
            self.get_logger().warning(f"gripper goal aborted: {reason}")
            goal_handle.abort()
            return result

        commanded = gripper_finger_target(value)
        measured = None
        deadline = time.monotonic() + self.gripper_settle
        while time.monotonic() < deadline:
            if goal_handle.is_cancel_requested:
                goal_handle.canceled()
                return result
            positions = self.controller.measured() or []
            measured = positions[N_ARM_JOINTS] if len(positions) > N_ARM_JOINTS else None
            if measured is not None and abs(measured - commanded) <= self.gripper_tolerance:
                break
            time.sleep(0.05)
        # fingers that stop early on an object are "stalled", which still succeeds, as with ros2_control's gripper
        result.position = measured if measured is not None else commanded
        result.reached_goal = measured is not None and abs(measured - commanded) <= self.gripper_tolerance
        result.stalled = not result.reached_goal
        goal_handle.succeed()
        return result


def main(args=None):
    token = os.environ.get("K1_WEB_TOKEN", "")
    if not token:
        print("K1_WEB_TOKEN is not set. Generate one with `openssl rand -hex 16` and export it.", file=sys.stderr)
        sys.exit(1)

    # uvicorn owns Ctrl+C; when it returns we shut ROS down ourselves
    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    node = K1WebNode()
    if len(token) < 16:
        node.get_logger().warning("K1_WEB_TOKEN is short, use at least 16 random characters")
    admin_password = os.environ.get("K1_WEB_ADMIN_PASSWORD", "").strip()
    if admin_password:
        node.get_logger().warning("K1_WEB_ADMIN_PASSWORD is set: it is accepted at login like the token")

    executor = MultiThreadedExecutor()
    executor.add_node(node)
    spin_thread = threading.Thread(target=executor.spin, daemon=True)
    spin_thread.start()

    config = WebConfig(token, node.static_dir, node.state_rate_hz, node.video_max_fps, node.secure_cookie,
                       admin_password)
    app = create_app(node.controller, node.poses, node.set_enabled, config)
    node.get_logger().info(f"serving on http://{node.host}:{node.port}")
    try:
        uvicorn.run(app, host=node.host, port=node.port, log_level="warning")
    finally:
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        spin_thread.join(timeout=1.0)


if __name__ == "__main__":
    main()
