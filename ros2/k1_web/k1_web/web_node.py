"""
ROS 2 node that serves the K1 web interface.

Bridges the web server (k1_web.server) to the k1_driver node:

    Subscribes:
        joint_states            sensor_msgs/JointState
        <image_topic>           sensor_msgs/CompressedImage (JPEG), default /image_raw/compressed
    Publishes:
        joint_position_command  std_msgs/Float64MultiArray  smoothed, rate limited, clamped to joint limits
        gripper_command         std_msgs/Float64
    Calls:
        enable                  std_srvs/SetBool

The auth token is read from the K1_WEB_TOKEN environment variable.
"""
import asyncio
import os
import sys
import threading

from ament_index_python.packages import get_package_share_directory
import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.signals import SignalHandlerOptions
from sensor_msgs.msg import CompressedImage, JointState
from std_msgs.msg import Float64, Float64MultiArray
from std_srvs.srv import SetBool
import uvicorn

from k1_web.control import N_ARM_JOINTS, ArmController
from k1_web.poses import PoseStore
from k1_web.server import WebConfig, create_app

ENABLE_TIMEOUT_SEC = 3.0


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
        poses_file = self.declare_parameter("poses_file", "").value or os.path.join(share, "config", "poses.yaml")
        user_poses_file = self.declare_parameter("user_poses_file", "~/.k1_web/poses.yaml").value
        self.secure_cookie = self.declare_parameter("secure_cookie", False).value
        self.static_dir = os.path.join(share, "static")

        if self.control_rate_hz <= 0.0 or self.state_rate_hz <= 0.0 or self.video_max_fps <= 0.0:
            raise ValueError("control_rate_hz, state_rate_hz and video_max_fps must be positive")

        self.controller = ArmController(limits_min, limits_max, max_joint_velocity,
                                        state_timeout_sec, deadman_timeout_sec)
        self.poses = PoseStore(poses_file, user_poses_file)
        self._warned_image_format = False

        self.arm_pub = self.create_publisher(Float64MultiArray, "joint_position_command", 10)
        self.gripper_pub = self.create_publisher(Float64, "gripper_command", 10)
        self.create_subscription(JointState, "joint_states", self.on_joint_state, 10)
        self.create_subscription(CompressedImage, image_topic, self.on_image, qos_profile_sensor_data)
        # reentrant so the service response is processed while other callbacks run
        self.enable_client = self.create_client(SetBool, "enable", callback_group=ReentrantCallbackGroup())
        self.create_timer(1.0 / self.control_rate_hz, self.on_control_timer)

        self.get_logger().info(
            f"limits min {limits_min} max {limits_max} rad, max velocity {max_joint_velocity} rad/s, "
            f"poses {self.poses.names()}")

    def on_joint_state(self, msg: JointState):
        self.controller.on_joint_state(msg.name, msg.position)

    def on_image(self, msg: CompressedImage):
        if "jpeg" not in msg.format.lower():
            if not self._warned_image_format:
                self.get_logger().warning(f"ignoring camera frames in format {msg.format!r}, need JPEG")
                self._warned_image_format = True
            return
        self.controller.on_image(bytes(msg.data))

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
        if response.success:
            self.controller.set_enabled(value)
        self.get_logger().info(f"enable({value}) from web: {response.message}")
        return response.success, response.message


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

    executor = MultiThreadedExecutor()
    executor.add_node(node)
    spin_thread = threading.Thread(target=executor.spin, daemon=True)
    spin_thread.start()

    config = WebConfig(token, node.static_dir, node.state_rate_hz, node.video_max_fps, node.secure_cookie)
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
