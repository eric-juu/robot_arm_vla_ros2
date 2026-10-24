
from unitree_k1_sdk import UnitreeK1Robot


if __name__ == "__main__":
    # arm = UnitreeK1Robot("/dev/ttyACM0")
    arm = UnitreeK1Robot("/dev/serial/by-path/pci-0000:00:14.0-usb-0:1.4:1.0")
    arm.connect()

    # enable the motors of the robot
    arm.enable()

    print("Present Position:", arm.joint_position_measured)

    # disable the motors of the robot
    # WARNING: the robot will collapse when the motors are disabled
    arm.disable()

