# k1_driver_py

ROS 2 driver node for the Unitree K1 arm. It wraps `unitree_k1_sdk.UnitreeK1Robot` (the `unitree_k1_sdk` package next to this one), so the serial protocol lives in one place.

This is the low-level layer: no joint limits and no smoothing. Normally `k1_web`'s bringup starts it, and you move the arm through `k1_web`'s trajectory and gripper actions instead (see the [k1_web README](../k1_web/README.md#ros-2-interface)).

Tested on ROS 2 Humble and Jazzy.

## Interface

| Name | Type | Description |
|---|---|---|
| `joint_states` (pub) | `sensor_msgs/JointState` | Measured positions, named as in the URDF (`k1_description`): `joint1` … `joint6` in radians, and `joint_gripper_left`, the finger position in metres (0 open, about −0.036 closed) |
| `enabled` (pub, latched) | `std_msgs/Bool` | Whether the motors are locked. Sent at startup and after every `enable` call |
| `joint_position_command` (sub) | `std_msgs/Float64MultiArray` | 6 arm joint targets in radians, in this order: `shoulder_pan, shoulder_lift, elbow_flex, wrist_roll, wrist_flex, gripper_roll` |
| `gripper_command` (sub) | `std_msgs/Float64` | `1.0` fully open, `0.0` fully closed (clamped) |
| `enable` (srv) | `std_srvs/SetBool` | `true` locks the motors, `false` releases them (**the arm collapses**) |

Commands that are the wrong length, non-finite, or out of the protocol's range are rejected with an error log and are not sent.

`joint_states` is only published once the arm has reported its state, and stops if the arm has been silent for `state_timeout_sec`. The node never publishes placeholder zeros.

Joint names map to the SDK's like this: `joint1` shoulder_pan, `joint2` shoulder_lift, `joint3` elbow_flex, `joint4` wrist_roll, `joint5` wrist_flex, `joint6` gripper_roll. The command topics keep the SDK's order.

The gripper's metres come from NXROBO's scale for this servo (`unitree_k1_sdk.gripper_finger_position`). The scale hasn't been checked against the real fingers yet.

## Parameters

See `config/k1_driver.yaml`. Notable ones:

- `port` (default `/dev/ttyACM0`). With the launch file, set it with the `port:=` argument, which overrides the YAML value.
- `command_duration_ms` (default `0`): the move time the firmware interpolates over for each arm command. `0` moves as fast as possible.
- `enable_on_start` / `disable_on_shutdown` (both default `false`)
- `joint_names`: the names published on `joint_states`. The defaults match the URDF, so `robot_state_publisher` can use them directly.

## Build and run

colcon builds this package together with the SDK (`unitree_k1_sdk`), so no pip step is needed. The repo root has its own `setup.py` (a pip install of the SDK alone), so point colcon at the `ros2/` folder with `--base-paths`. Otherwise colcon stops at the repo root and never finds the ROS packages.

The full setup is in the [top-level README](../../README.md#setup-once-on-the-linux-pc). In short, with the repo at `~/Downloads/robot_arm_vla_ros2`:

```bash
# dependencies
sudo apt install python3-serial python3-numpy python3-tqdm

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
ros2 topic echo /enabled --qos-durability transient_local
ros2 service call /enable std_srvs/srv/SetBool "{data: true}"
ros2 topic pub --once /joint_position_command std_msgs/msg/Float64MultiArray "{data: [0.3, 0.0, 0.0, 0.0, 0.0, 0.0]}"
ros2 topic pub --once /gripper_command std_msgs/msg/Float64 "{data: 0.0}"
```
