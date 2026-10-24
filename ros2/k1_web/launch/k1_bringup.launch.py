"""
Starts everything: the arm driver (or a mock), robot_state_publisher with the K1's URDF (TF), the wrist camera,
the web interface (which also serves the trajectory and gripper actions) and optionally RViz.

    ros2 launch k1_web k1_bringup.launch.py port:=/dev/ttyACM0
    ros2 launch k1_web k1_bringup.launch.py mock:=true use_camera:=false    # no hardware
    ros2 launch k1_web k1_bringup.launch.py tunnel:=true                    # also print a public https link
    ros2 launch k1_web k1_bringup.launch.py rviz:=true                      # 3D view of the arm (needs a display)

The web node needs K1_WEB_TOKEN in the environment.
"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition, UnlessCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import Command, LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    share = get_package_share_directory("k1_web")
    driver_launch = os.path.join(get_package_share_directory("k1_driver_py"), "launch", "k1_driver.launch.py")
    description = get_package_share_directory("k1_description")
    robot_description = ParameterValue(
        Command(["xacro ", os.path.join(description, "urdf", "k1.urdf.xacro"),
                 " mount_xyz:='", LaunchConfiguration("mount_xyz"), "'",
                 " mount_rpy:='", LaunchConfiguration("mount_rpy"), "'"]),
        value_type=str)

    return LaunchDescription([
        DeclareLaunchArgument("port", default_value="/dev/ttyACM0", description="serial port the arm is connected to"),
        DeclareLaunchArgument("mock", default_value="false", description="use a fake arm instead of k1_driver"),
        DeclareLaunchArgument("use_camera", default_value="true", description="start the v4l2 camera node"),
        DeclareLaunchArgument("video_device", default_value="/dev/video0", description="camera device"),
        DeclareLaunchArgument("tunnel", default_value="false",
                              description="expose the page through a Cloudflare quick tunnel (needs cloudflared)"),
        DeclareLaunchArgument("web_port", default_value="8080", description="port the web server listens on"),
        DeclareLaunchArgument("rviz", default_value="false", description="open RViz with the arm model"),
        DeclareLaunchArgument("mount_xyz", default_value="0 0 0",
                              description="arm base position in the world frame, metres"),
        DeclareLaunchArgument("mount_rpy", default_value="0 0 0",
                              description="arm base orientation in the world frame, radians"),
        DeclareLaunchArgument("web_params_file", default_value=os.path.join(share, "config", "k1_web.yaml")),
        DeclareLaunchArgument("camera_params_file", default_value=os.path.join(share, "config", "camera.yaml")),

        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(driver_launch),
            launch_arguments={"port": LaunchConfiguration("port")}.items(),
            condition=UnlessCondition(LaunchConfiguration("mock")),
        ),
        Node(
            package="k1_web", executable="mock_arm", name="mock_arm", output="screen",
            condition=IfCondition(LaunchConfiguration("mock")),
        ),
        Node(
            package="robot_state_publisher", executable="robot_state_publisher", name="robot_state_publisher",
            parameters=[{"robot_description": robot_description}],
        ),
        Node(
            package="rviz2", executable="rviz2", name="rviz2",
            arguments=["-d", os.path.join(description, "rviz", "k1.rviz")],
            condition=IfCondition(LaunchConfiguration("rviz")),
        ),
        Node(
            package="v4l2_camera", executable="v4l2_camera_node", name="camera", output="screen",
            parameters=[LaunchConfiguration("camera_params_file"),
                        {"video_device": LaunchConfiguration("video_device")}],
            condition=IfCondition(LaunchConfiguration("use_camera")),
        ),
        Node(
            package="k1_web", executable="web_node", name="k1_web", output="screen",
            parameters=[LaunchConfiguration("web_params_file"),
                        {"port": LaunchConfiguration("web_port")}],
        ),
        Node(
            package="k1_web", executable="tunnel", name="tunnel", output="screen",
            arguments=[["http://127.0.0.1:", LaunchConfiguration("web_port")]],
            condition=IfCondition(LaunchConfiguration("tunnel")),
        ),
    ])
