# k1_driver_py command reference

Every command for running and using the K1 driver node. Run them in a terminal where the workspace is sourced:

```bash
source /opt/ros/jazzy/setup.bash
source <workspace>/install/setup.bash
```

## Launching

```bash
# default (port /dev/ttyACM0, settings from config/k1_driver.yaml)
ros2 launch k1_driver_py k1_driver.launch.py

# different serial port
ros2 launch k1_driver_py k1_driver.launch.py port:=/dev/ttyACM1

# your own YAML file of parameters
ros2 launch k1_driver_py k1_driver.launch.py params_file:=/path/to/my_params.yaml

# show the launch arguments
ros2 launch k1_driver_py k1_driver.launch.py --show-args

# run the node directly, with parameters on the command line instead of the YAML
ros2 run k1_driver_py k1_driver --ros-args -p port:=/dev/ttyACM0 -p command_duration_ms:=1000 -p enable_on_start:=true
```

## Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `port` | `/dev/ttyACM0` | Serial port |
| `baudrate` | `1000000` | Serial baud rate |
| `publish_rate_hz` | `50.0` | How often `joint_states` is published |
| `command_duration_ms` | `0` | Time the firmware takes to reach each arm target (0 = as fast as possible, max 32767) |
| `state_timeout_sec` | `1.0` | Stop publishing if the arm has been silent this long (0 = never stop) |
| `enable_on_start` | `false` | Lock the motors when the node starts |
| `disable_on_shutdown` | `false` | Release the motors when the node stops (the arm collapses) |

**Parameters are only read at startup.** `ros2 param set` will appear to work but changes nothing. To change one, set it with `-p` or in the YAML and restart the node. If you edit the YAML inside the package, rebuild with `colcon build`. Alternatively, keep a copy elsewhere and pass it with `params_file:=`.

## Reading the arm state

```bash
ros2 topic echo /joint_states                    # continuous stream
ros2 topic echo /joint_states --once             # a single message
ros2 topic echo /joint_states --field position   # positions only
ros2 topic hz /joint_states                      # should be about 50 Hz
```

The joint order is `shoulder_pan, shoulder_lift, elbow_flex, wrist_roll, wrist_flex, gripper_roll, gripper`, with values in radians.

## Enabling and disabling the motors

```bash
ros2 service call /enable std_srvs/srv/SetBool "{data: true}"    # lock the motors, which hold position
ros2 service call /enable std_srvs/srv/SetBool "{data: false}"   # release the motors (support the arm first)
```

## Moving the arm

Send exactly 6 values in radians, in this order: `shoulder_pan, shoulder_lift, elbow_flex, wrist_roll, wrist_flex, gripper_roll`. The motors need to be enabled first.

```bash
# single target
ros2 topic pub --once /joint_position_command std_msgs/msg/Float64MultiArray "{data: [0.3, 0.0, 0.0, 0.0, 0.0, 0.0]}"

# back to zero
ros2 topic pub --once /joint_position_command std_msgs/msg/Float64MultiArray "{data: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]}"

# keep re-sending a target at 10 Hz until Ctrl+C
ros2 topic pub -r 10 /joint_position_command std_msgs/msg/Float64MultiArray "{data: [0.3, 0.0, 0.0, 0.0, 0.0, 0.0]}"
```

The node rejects a command and logs an error if it has the wrong number of values, contains NaN or inf, or is out of the protocol's range. The node itself does not enforce safe joint limits, so start with small values.

## Gripper

The value is clamped to the range 0.0–1.0.

```bash
ros2 topic pub --once /gripper_command std_msgs/msg/Float64 "{data: 1.0}"   # fully open
ros2 topic pub --once /gripper_command std_msgs/msg/Float64 "{data: 0.5}"   # half open
ros2 topic pub --once /gripper_command std_msgs/msg/Float64 "{data: 0.0}"   # fully closed
```

## Inspecting and debugging

```bash
ros2 node list                          # should show /k1_driver
ros2 node info /k1_driver               # its topics and services
ros2 topic list
ros2 topic info /joint_position_command
ros2 interface show std_msgs/msg/Float64MultiArray
ros2 param list /k1_driver
ros2 param get /k1_driver command_duration_ms
ros2 pkg executables k1_driver_py       # should show: k1_driver_py k1_driver
```

## Recording and replaying data

```bash
ros2 bag record /joint_states /joint_position_command /gripper_command -o my_run
ros2 bag info my_run
ros2 bag play my_run      # replays the commands too, so the arm will move
```

To watch a replay without moving the arm, play only the state topic: `ros2 bag play my_run --topics /joint_states`.

## Web interface and camera

To drive the arm from a browser (sliders, saved poses, live wrist camera, remote access over Tailscale), use the `k1_web` package. It starts this driver for you:

```bash
export K1_WEB_TOKEN=$(openssl rand -hex 16)
ros2 launch k1_web k1_bringup.launch.py port:=/dev/ttyACM0
```

See `ros2/k1_web/README.md`.
