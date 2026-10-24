# unitree_k1_sdk

The serial driver library for the Unitree K1 (Sagittarius) arm: the `0x55 0xAA … 0x7D` protocol, the receive thread, and joint and gripper commands. It is plain Python with no ROS imports. It's packaged as a ROS package so `colcon build` installs it for `k1_driver_py`, with no separate pip step.

```python
from unitree_k1_sdk import UnitreeK1Robot

arm = UnitreeK1Robot("/dev/ttyACM0")
arm.connect()
print(arm.joint_position_measured)   # 7 values: 6 joints in radians + the gripper servo
```

Joint order: `shoulder_pan, shoulder_lift, elbow_flex, wrist_roll, wrist_flex, gripper_roll, gripper`. In the URDF (`k1_description`) these are `joint1` … `joint6` and `joint_gripper_left`.

Gripper helpers:
- `gripper_finger_position(rad)` converts the reported gripper value to finger metres (0 open, negative closed, as in the URDF);
- `gripper_open_fraction(m)` and `gripper_finger_target(value)` convert between metres and `set_gripper()`'s 1.0 open / 0.0 closed.

The metres scale comes from NXROBO's driver and hasn't been calibrated on the K1 yet.

## Examples (without ROS)

`examples/` talks to the serial port directly. **Stop the ROS driver first**: only one program can own the port, and these scripts bypass every limit `k1_web` enforces. For normal use, prefer the ROS examples in `k1_examples`.

```bash
python3 examples/demo.py /dev/ttyACM0
```

Outside the ROS workspace, install the SDK with pip from the repo root: `pip install -e .`

## Tests

```bash
python3 -m pytest test
```
