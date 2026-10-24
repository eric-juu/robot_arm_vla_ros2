import time

from unitree_k1_sdk import UnitreeK1Robot, ZERO_POSE, IDLE_POSE


if __name__ == "__main__":
    # arm = UnitreeK1Robot("/dev/ttyACM0")
    arm = UnitreeK1Robot("/dev/serial/by-path/pci-0000:00:14.0-usb-0:1.4:1.0")
    arm.connect()

    print("going to zero pose in 3 seconds...")
    time.sleep(3)

    print("going to zero pose...")
    arm.step(ZERO_POSE)

    print("going to idle pose in 3 seconds...")
    time.sleep(3)

    print("going to idle pose...")
    arm.step(IDLE_POSE)

    print("disabling in 3 seconds...")
    time.sleep(3)

    # disable the motors of the robot
    # WARNING: the robot will collapse when the motors are disabled
    print("disabling...")
    arm.disable()


