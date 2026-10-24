"""
Fake K1 arm for testing the web interface without hardware.

Offers the same interface as k1_driver (joint_states with the URDF joint names, enabled, joint_position_command,
gripper_command, enable).
The joints follow the last command with a little lag; commands are ignored while disabled.
"""
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from sensor_msgs.msg import JointState
from std_msgs.msg import Bool, Float64, Float64MultiArray
from std_srvs.srv import SetBool

from unitree_k1_sdk import gripper_finger_target

JOINT_NAMES = ["joint1", "joint2", "joint3", "joint4", "joint5", "joint6", "joint_gripper_left"]
PUBLISH_RATE_HZ = 50.0
FOLLOW_GAIN = 0.3   # fraction of the remaining error closed per publish


class MockArmNode(Node):

    def __init__(self):
        super().__init__("mock_arm")
        self.enabled = False
        self.position = [0.0] * len(JOINT_NAMES)
        self.target = list(self.position)

        self.joint_state_pub = self.create_publisher(JointState, "joint_states", 10)
        self.enabled_pub = self.create_publisher(Bool, "enabled",
                                                 QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self.enabled_pub.publish(Bool(data=False))
        self.create_subscription(Float64MultiArray, "joint_position_command", self.on_joint_position_command, 10)
        self.create_subscription(Float64, "gripper_command", self.on_gripper_command, 10)
        self.create_service(SetBool, "enable", self.on_enable)
        self.create_timer(1.0 / PUBLISH_RATE_HZ, self.publish_joint_states)
        self.get_logger().info("mock arm ready")

    def on_joint_position_command(self, msg: Float64MultiArray):
        if self.enabled and len(msg.data) == 6:
            self.target[:6] = list(msg.data)

    def on_gripper_command(self, msg: Float64):
        if self.enabled:
            self.target[6] = gripper_finger_target(min(max(msg.data, 0.0), 1.0))    # finger metres, like the driver

    def on_enable(self, request: SetBool.Request, response: SetBool.Response):
        self.enabled = request.data
        if not self.enabled:
            self.target = list(self.position)
        response.success = True
        response.message = "motors enabled" if request.data else "motors disabled"
        self.enabled_pub.publish(Bool(data=self.enabled))
        self.get_logger().info(response.message)
        return response

    def publish_joint_states(self):
        self.position = [p + FOLLOW_GAIN * (t - p) for p, t in zip(self.position, self.target)]
        msg = JointState()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.name = list(JOINT_NAMES)
        msg.position = list(self.position)
        self.joint_state_pub.publish(msg)


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = MockArmNode()
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
