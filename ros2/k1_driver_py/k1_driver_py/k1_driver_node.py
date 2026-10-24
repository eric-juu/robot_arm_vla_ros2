"""
ROS 2 driver node for the Unitree K1 arm.

Wraps `unitree_k1_sdk.UnitreeK1Robot` and exposes it over ROS 2:

    Publishes:
        joint_states            sensor_msgs/JointState      measured positions, with the URDF joint names
                                                            (k1_description): joint1..joint6 in radians and
                                                            joint_gripper_left (finger position in metres)
        enabled                 std_msgs/Bool               whether the motors are locked; latched
    Subscribes:
        joint_position_command  std_msgs/Float64MultiArray  6 arm joint targets in radians
        gripper_command         std_msgs/Float64            1.0 fully open, 0.0 fully closed
    Services:
        enable                  std_srvs/SetBool            true locks the motors, false releases them
"""
import math
import struct
import time

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
import serial
from sensor_msgs.msg import JointState
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_msgs.msg import Bool, Float64, Float64MultiArray
from std_srvs.srv import SetBool

from unitree_k1_sdk.k1 import JOINT_NAMES, UnitreeK1Robot, gripper_finger_position


N_ARM_JOINTS = 6        # the first 6 entries of JOINT_NAMES; the 7th is the gripper
# joint names in k1_description's URDF, in the SDK's order
URDF_JOINT_NAMES = ["joint1", "joint2", "joint3", "joint4", "joint5", "joint6", "joint_gripper_left"]
# late subscribers (k1_web, a restarted script) get the last value
LATCHED = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
MAX_DURATION_MS = 32767  # the move duration is sent as a signed 16 bit integer


class K1DriverNode(Node):

    def __init__(self):
        super().__init__("k1_driver")

        self.port = self.declare_parameter("port", "/dev/ttyACM0").value
        self.baudrate = self.declare_parameter("baudrate", 1_000_000).value
        self.publish_rate_hz = self.declare_parameter("publish_rate_hz", 50.0).value
        self.command_duration_ms = self.declare_parameter("command_duration_ms", 0).value
        self.state_timeout_sec = self.declare_parameter("state_timeout_sec", 1.0).value
        self.enable_on_start = self.declare_parameter("enable_on_start", False).value
        self.disable_on_shutdown = self.declare_parameter("disable_on_shutdown", False).value
        self.joint_names = list(self.declare_parameter("joint_names", URDF_JOINT_NAMES).value)

        if self.publish_rate_hz <= 0.0:
            raise ValueError(f"publish_rate_hz must be positive, got {self.publish_rate_hz}")
        if not 0 <= self.command_duration_ms <= MAX_DURATION_MS:
            raise ValueError(f"command_duration_ms must be in [0, {MAX_DURATION_MS}], got {self.command_duration_ms}")
        if len(self.joint_names) != len(JOINT_NAMES):
            raise ValueError(f"joint_names needs {len(JOINT_NAMES)} names, got {self.joint_names}")

        # open the serial port last, so a bad parameter never leaves the port open
        self.arm = UnitreeK1Robot(self.port, self.baudrate)
        self.arm.connect()
        self.get_logger().info(f"connected to K1 on {self.port} at {self.baudrate} baud")

        self.joint_state_pub = self.create_publisher(JointState, "joint_states", 10)
        self.enabled_pub = self.create_publisher(Bool, "enabled", LATCHED)
        if self.enable_on_start:
            self.arm.enable()
            self.get_logger().info("motors enabled")
        self.enabled_pub.publish(Bool(data=self.arm.is_enabled))

        self.create_subscription(Float64MultiArray, "joint_position_command", self.on_joint_position_command, 10)
        self.create_subscription(Float64, "gripper_command", self.on_gripper_command, 10)
        self.create_service(SetBool, "enable", self.on_enable)
        self.create_timer(1.0 / self.publish_rate_hz, self.publish_joint_states)

    def publish_joint_states(self):
        timestamp = self.arm.joint_position_timestamp
        if timestamp is None:
            self.get_logger().warning("no joint state received from the arm yet", throttle_duration_sec=5.0)
            return
        age = time.monotonic() - timestamp
        if self.state_timeout_sec > 0.0 and age > self.state_timeout_sec:
            # don't republish old positions with a fresh stamp
            self.get_logger().warning(f"last joint state from the arm is {age:.2f} s old", throttle_duration_sec=5.0)
            return

        msg = JointState()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.name = list(self.joint_names)
        positions = self.arm.joint_position_measured.tolist()
        positions[N_ARM_JOINTS] = gripper_finger_position(positions[N_ARM_JOINTS])
        msg.position = positions
        self.joint_state_pub.publish(msg)

    def on_joint_position_command(self, msg: Float64MultiArray):
        positions = list(msg.data)
        if len(positions) != N_ARM_JOINTS:
            self.get_logger().error(
                f"joint_position_command needs {N_ARM_JOINTS} values {JOINT_NAMES[:N_ARM_JOINTS]}, got {len(positions)}")
            return
        if not all(math.isfinite(p) for p in positions):
            self.get_logger().error(f"joint_position_command contains non-finite values: {positions}")
            return

        try:
            # sends only the 6 arm joints; step() would also re-send the gripper
            self.arm._send_arm_all_server_time(self.command_duration_ms, *positions)
        except struct.error:
            self.get_logger().error(f"joint_position_command values out of range: {positions}")
        except serial.SerialException as e:
            self.get_logger().error(f"failed to send joint command: {e}")

    def on_gripper_command(self, msg: Float64):
        value = msg.data
        if not math.isfinite(value):
            self.get_logger().error(f"gripper_command is not finite: {value}")
            return
        value = min(max(value, 0.0), 1.0)

        try:
            self.arm.set_gripper(value)
        except serial.SerialException as e:
            self.get_logger().error(f"failed to send gripper command: {e}")

    def on_enable(self, request: SetBool.Request, response: SetBool.Response):
        try:
            if request.data:
                self.arm.enable()
            else:
                self.arm.disable()
        except serial.SerialException as e:
            response.success = False
            response.message = f"serial error: {e}"
            return response

        response.success = True
        response.message = "motors enabled" if request.data else "motors disabled"
        self.enabled_pub.publish(Bool(data=self.arm.is_enabled))
        self.get_logger().info(response.message)
        return response

    def shutdown(self):
        if not self.arm.is_connected:
            return
        if self.disable_on_shutdown:
            try:
                self.arm.disable()
                time.sleep(0.05)    # let the command go out before closing the port
            except serial.SerialException:
                pass
        self.arm.disconnect()


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = K1DriverNode()
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        if node is not None:
            node.shutdown()
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
