# k1_driver_py

ROS 2 driver node for the K1. It wraps `unitree_k1_sdk.UnitreeK1Robot`, so the serial protocol lives in one place. This is the low level: no joint limits and no smoothing. k1_web's bringup starts it, and normal motion goes through k1_web's actions ([k1_web README](../k1_web/README.md#ros-2-interface)). Tested on Humble and Jazzy.

## Interface

| Name | Type | Description |
|---|---|---|
| `joint_states` (pub) | `sensor_msgs/JointState` | measured positions named as in the URDF: `joint1` … `joint6` (rad) and `joint_gripper_left` (finger position in metres: 0 open, about −0.036 closed). Only published once the arm has reported, and stops if it goes silent for `state_timeout_sec`; never placeholder zeros |
| `enabled` (pub, latched) | `std_msgs/Bool` | whether the motors are locked; sent at startup and after every `enable` call |
| `joint_position_command` (sub) | `std_msgs/Float64MultiArray` | 6 arm targets in rad, in the SDK's order: `shoulder_pan, shoulder_lift, elbow_flex, wrist_roll, wrist_flex, gripper_roll`. Wrong length, non-finite or out-of-range commands are rejected with an error log |
| `gripper_command` (sub) | `std_msgs/Float64` | 1.0 fully open … 0.0 closed (clamped) |
| `enable` (srv) | `std_srvs/SetBool` | `true` locks the motors, `false` releases them (**the arm collapses**) |

`joint1` … `joint6` are `shoulder_pan, shoulder_lift, elbow_flex, wrist_roll, wrist_flex, gripper_roll`. The gripper's metres come from NXROBO's scale for this servo (`unitree_k1_sdk.gripper_finger_position`), not yet calibrated on the K1.

## Parameters

Set in `config/k1_driver.yaml`, with `-p name:=value`, or with your own file (`params_file:=`). They are read only at startup: `ros2 param set` appears to work but changes nothing, so restart the node.

| Parameter | Default | Meaning |
|---|---|---|
| `port` | `/dev/ttyACM0` | serial port (the launch file's `port:=` overrides it) |
| `baudrate` | `1000000` | serial baud rate |
| `publish_rate_hz` | `50.0` | how often `joint_states` is published |
| `command_duration_ms` | `0` | time the firmware interpolates over per arm target (0 = as fast as possible; keep 0 under k1_web, which smooths itself) |
| `state_timeout_sec` | `1.0` | stop publishing if the arm has been silent this long (0 = never) |
| `enable_on_start` / `disable_on_shutdown` | `false` | lock the motors at start / release them at shutdown (the arm collapses) |
| `joint_names` | `[joint1, …, joint6, joint_gripper_left]` | names published on `joint_states`, in the SDK's order; the defaults match the URDF |

## Running it alone

```bash
ros2 launch k1_driver_py k1_driver.launch.py port:=/dev/ttyACM0
ros2 topic hz /joint_states                                          # about 50 Hz
ros2 service call /enable std_srvs/srv/SetBool "{data: true}"
ros2 topic pub --once /joint_position_command std_msgs/msg/Float64MultiArray "{data: [0.3, 0, 0, 0, 0, 0]}"
ros2 topic pub --once /gripper_command std_msgs/msg/Float64 "{data: 0.0}"
ros2 topic echo /enabled --qos-durability transient_local
```

The raw command topics bypass every limit k1_web enforces: start with small values, with someone at the arm. `ros2 bag play` of a recording replays its commands too; play only `--topics /joint_states` to watch without moving. The user needs the `dialout` group for the serial port.
