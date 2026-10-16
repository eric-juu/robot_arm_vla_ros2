# k1_driver_py

ROS 2 driver node for the Unitree K1 arm. It wraps `unitree_k1_sdk.UnitreeK1Robot`, so the serial protocol lives in one place.

Tested on ROS 2 Humble and Jazzy.

## Interface

| Name | Type | Description |
|---|---|---|
| `joint_states` (pub) | `sensor_msgs/JointState` | Measured positions of all 7 joints, in radians |
| `joint_position_command` (sub) | `std_msgs/Float64MultiArray` | 6 arm joint targets in radians, in this order: `shoulder_pan, shoulder_lift, elbow_flex, wrist_roll, wrist_flex, gripper_roll` |
| `gripper_command` (sub) | `std_msgs/Float64` | `1.0` fully open, `0.0` fully closed (clamped) |
| `enable` (srv) | `std_srvs/SetBool` | `true` locks the motors, `false` releases them (**the arm collapses**) |

Commands that are the wrong length, non-finite, or out of the protocol's range are rejected with an error log and are not sent.

`joint_states` is only published once the arm has reported its state, and stops if the arm has been silent for `state_timeout_sec`. The node never publishes placeholder zeros.

The 7th `joint_states` entry (`gripper`) is the raw value the arm reports for that servo, converted the same way as the other joints. Its units have not been checked against the real gripper yet.

## Parameters

See `config/k1_driver.yaml`. Notable ones:

- `port` (default `/dev/ttyACM0`). With the launch file, set it with the `port:=` argument, which overrides the YAML value.
- `command_duration_ms` (default `0`): the move time the firmware interpolates over for each arm command. `0` moves as fast as possible.
- `enable_on_start` / `disable_on_shutdown` (both default `false`)

## Build and run

The SDK is installed with pip, and the ROS package is built with colcon. The repo root has its own `setup.py`, so point colcon at the `ros2/` folder with `--base-paths`. Otherwise colcon stops at the repo root and never finds this package.

The full setup is in the [top-level README](../../README.md#setup-once-on-the-linux-pc). In short, with the repo at `~/Downloads/robot_arm_vla_ros2`:

```bash
# dependencies
sudo apt install python3-serial python3-numpy python3-tqdm
pip install --no-deps -e ~/Downloads/robot_arm_vla_ros2 --break-system-packages   # leave out the flag on Humble

# build
source /opt/ros/jazzy/setup.bash
mkdir -p ~/ros2_ws && cd ~/ros2_ws
colcon build --base-paths ~/Downloads/robot_arm_vla_ros2/ros2
source install/setup.bash

# run
ros2 launch k1_driver_py k1_driver.launch.py port:=/dev/ttyACM0
```

The user running the node needs access to the serial port (`sudo usermod -aG dialout $USER`, then log in again).

## Examples

```bash
ros2 topic echo /joint_states
ros2 service call /enable std_srvs/srv/SetBool "{data: true}"
ros2 topic pub --once /joint_position_command std_msgs/msg/Float64MultiArray "{data: [0.3, 0.0, 0.0, 0.0, 0.0, 0.0]}"
ros2 topic pub --once /gripper_command std_msgs/msg/Float64 "{data: 0.0}"
```
