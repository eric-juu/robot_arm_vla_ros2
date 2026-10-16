"""
Starts everything for remote control: the arm driver (or a mock), the wrist camera and the web interface.

    ros2 launch k1_web k1_bringup.launch.py port:=/dev/ttyACM0
    ros2 launch k1_web k1_bringup.launch.py mock:=true use_camera:=false    # no hardware

The web node needs K1_WEB_TOKEN in the environment.
"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition, UnlessCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    share = get_package_share_directory("k1_web")
    driver_launch = os.path.join(get_package_share_directory("k1_driver_py"), "launch", "k1_driver.launch.py")

    return LaunchDescription([
        DeclareLaunchArgument("port", default_value="/dev/ttyACM0", description="serial port the arm is connected to"),
        DeclareLaunchArgument("mock", default_value="false", description="use a fake arm instead of k1_driver"),
        DeclareLaunchArgument("use_camera", default_value="true", description="start the v4l2 camera node"),
        DeclareLaunchArgument("video_device", default_value="/dev/video0", description="camera device"),
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
            package="v4l2_camera", executable="v4l2_camera_node", name="camera", output="screen",
            parameters=[LaunchConfiguration("camera_params_file"),
                        {"video_device": LaunchConfiguration("video_device")}],
            condition=IfCondition(LaunchConfiguration("use_camera")),
        ),
        Node(
            package="k1_web", executable="web_node", name="k1_web", output="screen",
            parameters=[LaunchConfiguration("web_params_file")],
        ),
    ])
