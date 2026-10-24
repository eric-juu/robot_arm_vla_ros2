# k1_examples

Example ROS 2 programs for the K1. They use the same interfaces as any ROS tool:
- `k1_web`'s `FollowJointTrajectory` and `GripperCommand` actions;
- the driver's `enable` service;
- `joint_states`.

Every move gets `k1_web`'s joint limits and speed limit, and STOP on the web page aborts it.

Start the bringup first (`ros2 launch k1_web k1_bringup.launch.py ...`, or `mock:=true` without an arm), and enable the motors on the web page.

```bash
ros2 run k1_examples move 0.3 0 0 0 0 0              # all 6 joints, radians
ros2 run k1_examples move joint1=0.3 joint5=-0.2     # some joints; the others stay put
ros2 run k1_examples move shoulder_pan=0.3 --time 5  # the SDK's joint names work too
ros2 run k1_examples gripper close                   # open, close, or 0.0 .. 1.0
ros2 run k1_examples zero                            # all joints to 0
ros2 run k1_examples demo                            # base left/right, elbow, gripper, back to the start
ros2 run k1_examples demo --enable                   # let the demo enable the motors
```

A refused or failed goal prints the reason (for example `outside k1_web's joint limits: ['shoulder_pan']`) and exits with code 1. Ctrl+C cancels the running goal, and the arm holds where it is. None of the programs disables the motors, because the arm would fall.

`k1_examples/arm.py` (`K1Arm`) is a small blocking client you can copy into your own scripts:

```python
from k1_examples.arm import K1Arm

with K1Arm() as arm:
    arm.move({"joint1": 0.3, "joint3": 0.2}, seconds=2.0)
    arm.gripper(0.0)
    print(arm.joint_positions())
```
