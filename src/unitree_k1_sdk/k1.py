import io
import time
import threading
import struct
import math

from tqdm import tqdm
import serial
import numpy as np


HALF_TURN_DEGREE = 180



DEFAULT_BAUDRATE = 1_000_000
DEFAULT_TIMEOUT = 2           # seconds

MOTOR_RESOLUTION = 4096


# Message start and end identifier specified in the UART protocol
MSG_START = b"\x55\xAA"
MSG_END = b"\x7D"


# these two value defines the upper and lower bounds of the gripper servo
# these are the raw values that the gripper servo accepts
# note that since the gripper is converting rotation to linear motion, the
# relation is not quite linear
GRIPPER_CLOSE_VALUE: int = 1250
GRIPPER_OPEN_VALUE: int = 0

# JOINT_NAMES = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_roll", "wrist_flex", "gripper_roll", "gripper_open"]
JOINT_NAMES = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_roll", "wrist_flex", "gripper_roll", "gripper"]

ZERO_POSE = np.array([
    0., 0., 0., 0., 0., 0., 0.
])

IDLE_POSE = np.array([
    0, 0, 0, 0.0, 0.0, 0.0, 0
])

class Message:
    TYPE_REQUEST_MESSAGE        = 0x01

class Command:
    CMD_CONTROL_END_ACTION      = 0x03
    CMD_CONTROL_ID_DEGREE       = 0x04
    CMD_CONTROL_ALL_DEGREE      = 0x05
    CMD_REPORT_SERVO_STATE      = 0x06      # 舵机状态
    CMD_CONTROL_LOCK_OR_FREE    = 0x08
    CMD_GET_VERSION             = 0x09      # 获取版本
    CMD_UPGRADE                 = 0x0A      # 升级相关的命令
    CMD_CONTROL_ALL_DEGREE_CB   = 0x0B 		# 插补控制命令
    CMD_CONTROL_ALL_DEGREE_AND_DIFF_TIME = 0x0C
    CMD_SET_SERVO_VEL           = 0x0D		# 设置舵机的插补速度
    CMD_SET_SERVO_ACC           = 0x0E		# 设置舵机的加速度
    CMD_SET_SERVO_TORQUE        = 0x0F		# 设置舵机的扭矩厌大小
    CMD_GET_SERVO_RT_INFO       = 0x10      # 获取舵机实时信息


class UnitreeK1Robot:
    """
    A Python interface for the Unitree K1 robotic arm.

    Attributes:
        port (str): The serial port to connect to the robot.
        baudrate (int): The baudrate to use for the serial connection.
    """

    @staticmethod
    def _rad_to_degree_int(rad: float) -> int:
        return int(1800. / math.pi * rad)

    @staticmethod
    def _calculate_checksum(n_bytes: int, message: bytes) -> bytes:
        sum = 0
        for i in range(n_bytes):
            sum += message[i]
        sum = sum & 0xFF
        return struct.pack("<B", sum)
    
    def __init__(self, port: str, baudrate: int = DEFAULT_BAUDRATE):
        self.port = port
        self.baudrate = baudrate

        self.port_handler = None
        self.is_connected = False
        self.is_enabled = False
        self.logs = {}
        self.update_thread = None

        self.joint_position_measured = np.zeros(7)
        self.joint_position_target = np.zeros(7)
        # time.monotonic() of the last servo state report, None until the first one arrives
        self.joint_position_timestamp = None

        self.calibration = None

    
    def connect(self):
        """
        Connect to the robot
        """
        if self.is_connected:
            raise RuntimeError("This robot device is already connected. Do not call `robot.connect()` twice.")
        
        self.port_handler = serial.Serial(self.port, self.baudrate, timeout=DEFAULT_TIMEOUT)
        # self.packet_handler
        self.is_connected = True

        self.start_receive_thread()
    
    def disconnect(self):
        """
        Disconnect from the robot
        """
        if not self.is_connected:
            raise RuntimeError("This robot device is not connected. Do not call `robot.disconnect()` twice.")
        self.is_connected = False
        self.update_thread.join()
        self.port_handler.close()
        
    def reconnect(self):
        """
        Reconnect to the robot with the latest settings
        """
        if self.is_connected:
            self.disconnect()
        self.connect()
        
    def receive_handler(self):
        print("[K1] <RxThread> receive handler started")
        while self.is_connected:
            try:
                self.parse_datagram()
            except Exception as e:
                # keep the thread alive so joint_position_measured keeps updating
                print(f"[K1] <RxThread> error while parsing datagram: {e!r}")
            # print("[K1] <RxThread> thread received datagram")
    
    def start_receive_thread(self):
        self.update_thread = threading.Thread(target=self.receive_handler, daemon=True)
        self.update_thread.start()

        # wait for at least one datagram to be received
        time.sleep(0.1)

    def parse_datagram(self):
        # scan for the start identifier, discarding any bytes before it, so that we
        # resynchronize instead of crashing if we join the stream mid-packet
        prev_byte = b""
        while self.is_connected:
            byte = self.port_handler.read(1)
            if not byte:
                return None     # read timed out
            if prev_byte + byte == MSG_START:
                break
            prev_byte = byte
        else:
            return None

        n_bytes_bytes = self.port_handler.read(1)
        if not n_bytes_bytes:
            return None
        n_bytes = n_bytes_bytes[0]

        # payload, checksum and end identifier
        rest = self.port_handler.read(n_bytes + 2)
        if len(rest) != n_bytes + 2:
            return None
        message = rest[:n_bytes]
        checksum = rest[n_bytes:n_bytes + 1]
        msg_end = rest[n_bytes + 1:]

        checksum_expected = self._calculate_checksum(n_bytes, message)

        # drop corrupted or misaligned packets; the next call will search for a new start identifier
        if msg_end != MSG_END or checksum != checksum_expected or n_bytes < 2:
            return None

        cmd_type = message[1]
        if cmd_type == Command.CMD_UPGRADE:
            print("升级的命令")
        elif cmd_type == Command.CMD_GET_VERSION:
            if message[0] == 0x02:
                print("version is %s" % message[2])
        elif cmd_type == Command.CMD_REPORT_SERVO_STATE:
            if message[0] == 0x01:
                joint_0_raw = struct.unpack("<h", message[2:4])[0]
                joint_1_raw = struct.unpack("<h", message[4:6])[0]
                joint_2_raw = struct.unpack("<h", message[6:8])[0]
                joint_3_raw = struct.unpack("<h", message[8:10])[0]
                joint_4_raw = struct.unpack("<h", message[10:12])[0]
                joint_5_raw = struct.unpack("<h", message[12:14])[0]
                joint_6_raw = struct.unpack("<h", message[14:16])[0]

                joint_raw = np.array([
                    joint_0_raw,
                    joint_1_raw,
                    joint_2_raw,
                    joint_3_raw,
                    joint_4_raw,
                    joint_5_raw,
                    joint_6_raw
                ], dtype=np.float32)

                self.joint_position_measured[:] = joint_raw / 1800.0 * np.pi
                self.joint_position_timestamp = time.monotonic()

        elif cmd_type == Command.CMD_GET_SERVO_RT_INFO:
            print(message[0])
            if message[0] == 0x02:
                print("servo is respone CMD_GET_SERVO_RT_INFO")
                # servo_state.servo_id = mFrameBuffer[5];
                # servo_state.speed = mFrameBuffer[6]|(mFrameBuffer[7]<<8);
                # servo_state.payload = mFrameBuffer[8]|(mFrameBuffer[9]<<8);
                # servo_state.voltage = mFrameBuffer[10];
                # servo_state.current = mFrameBuffer[11]|(mFrameBuffer[12]<<8);
                # servo_state.flag = 1;
        else:
            print("unknown command")
            print(message)

        return message

    def send_message(self, message: bytes):
        n_bytes = len(message)
        checksum = self._calculate_checksum(n_bytes, message)
        n_bytes_bytes = struct.pack("<B", n_bytes)
        buffer = MSG_START + n_bytes_bytes + message + checksum + MSG_END
        self.port_handler.write(buffer)
    
    def _set_arm_locked(self, locked: bool):
        """
        int CSDarmCommonSerial::SendArmLockOrFree(unsigned char onoff)
        """
        locked = 1 if locked else 0
        message = struct.pack("<BBB", Message.TYPE_REQUEST_MESSAGE, Command.CMD_CONTROL_LOCK_OR_FREE, locked)
        self.send_message(message)

    def enable(self):
        """
        Locks the arm by enabling the servo control.
        """
        self._set_arm_locked(True)
        self.is_enabled = True
    
    def disable(self):
        """
        Unlocks the arm by disabling the servo control.
        """
        self._set_arm_locked(False)
        self.is_enabled = False
    
    def _set_gripper_raw(self, is_open: bool, value: int):
        """
        int CSDarmCommonSerial::SendArmEndAction(unsigned char onoff, short value)
        """
        is_open = 1 if is_open else 0
        # clip actuator range
        value = max(GRIPPER_OPEN_VALUE, min(value, GRIPPER_CLOSE_VALUE))

        message = struct.pack("<BBBH",
                              Message.TYPE_REQUEST_MESSAGE,
                              Command.CMD_CONTROL_END_ACTION,
                              is_open,
                              value)
        self.send_message(message)
    
    def set_gripper(self, value: float):
        """
        Sets the gripper open state.

        Args:
            value (float): The gripper open state. 1.0 is fully open, 0.0 is fully closed.
        """
        is_open = value > 0.5
        value = GRIPPER_OPEN_VALUE + ((1 - value) * (GRIPPER_CLOSE_VALUE - GRIPPER_OPEN_VALUE))
        value = int(value)
        
        self._set_gripper_raw(is_open, value)
    
    def set_gripper_rad(self, rad: float):
        """
        Sets the gripper open angle.

        Args:
            rad (float): The gripper open angle in radians. 0 rad is fully open, 2.0944 rad (120 deg) is fully closed.
        """
        value = rad / 2.0944
        self.set_gripper(1.0 - rad)
    

    def _send_arm_all_server_time(self, difftime: int, v1: float, v2: float, v3: float, v4: float, v5: float, v6: float):
        """
        int CSDarmCommonSerial::SendArmAllServerTime(short difftime, float v1, float v2, float v3, float v4, float v5, float v6)

        Args:
            difftime (int): The time spend to move to the target position, in milliseconds.
            v1~v6 (float): The target angles of the 6 joints, in radians.
        """
        message = struct.pack("<BB",
                              Message.TYPE_REQUEST_MESSAGE,
                              Command.CMD_CONTROL_ALL_DEGREE_AND_DIFF_TIME)
        
        message += struct.pack("<h", difftime)
        message += struct.pack("<h", self._rad_to_degree_int(v1))
        message += struct.pack("<h", self._rad_to_degree_int(v2))
        message += struct.pack("<h", self._rad_to_degree_int(v3))
        message += struct.pack("<h", self._rad_to_degree_int(v4))
        message += struct.pack("<h", self._rad_to_degree_int(v5))
        message += struct.pack("<h", self._rad_to_degree_int(v6))
        
        self.send_message(message)
    
    def update(self):
        self.parse_datagram()

    def send_joint_position_target(self, duration_ms: int = 0):
        """
        Writes the target joint positions to the robot

        Args:
            duration_ms (int): The time the arm takes to reach the target, in milliseconds.
                0 moves there as fast as possible.
        """
        self._send_arm_all_server_time(
            duration_ms,
            self.joint_position_target[0],
            self.joint_position_target[1],
            self.joint_position_target[2],
            self.joint_position_target[3],
            self.joint_position_target[4],
            self.joint_position_target[5]
        )
        self.set_gripper_rad(self.joint_position_target[6])

    def step(self, actions: np.ndarray, duration_ms: int = 0):
        self.joint_position_target[:] = actions
        self.send_joint_position_target(duration_ms)
        

    # ====== lerobot compatibility ======
    def are_motors_configured(self) -> bool:
        """
        Check if the motors are configured correctly.

        This function provides compatibility with the `lerobot` library.

        Returns:
            bool: True if the motors are configured correctly, False otherwise
        """
        return True
    
    @property
    def motor_names(self) -> list[str]:
        return JOINT_NAMES
    
    @property
    def motor_models(self) -> list[str]:
        return ["x_series"] * 7
    
    @property
    def motor_indices(self) -> list[int]:
        return list(range(7))
    
    def read(self, data_name: str, motor_names: str | list[str] | None = None) -> np.ndarray:
        if data_name == "Torque_Enable":
            return np.array([1 if self.is_enabled else 0] * 7)
        elif data_name == "Present_Position":
            # convert from radians to the raw encoder counts, 4096 counts per full rotation
            values = self.joint_position_measured * MOTOR_RESOLUTION / (2 * np.pi)
        elif data_name == "Goal_Position":
            values = self.joint_position_target * MOTOR_RESOLUTION / (2 * np.pi)
        else:
            raise ValueError(f"Unknown data name: {data_name}")
    
        if self.calibration is not None:
            values = self.apply_calibration_autocorrect(values)
        
        return values
        
    def write(self, data_name: str, values: int | float | np.ndarray, motor_names: str | list[str] | None = None) -> None:
        if data_name == "Torque_Enable":
            if values:
                print("[K1] enabling torque")
                self.enable()
                time.sleep(0.02)  # delay some time to make sure the robot is enabled
            else:
                print("[K1] disabling torque")
                self.disable()
                time.sleep(0.02)  # delay some time to make sure the robot is disabled
            return
        elif data_name == "Goal_Position":
            if self.calibration is not None:
                values = self.revert_calibration(values)
            values = values * (2 * np.pi) / MOTOR_RESOLUTION
            self.joint_position_target[0] = values[0]
            self.joint_position_target[1] = values[1]
            self.joint_position_target[2] = values[2]
            self.joint_position_target[3] = values[3]
            self.joint_position_target[4] = values[4]
            self.joint_position_target[5] = values[5]
            self.joint_position_target[6] = values[6] - 0.2 * np.pi    # offset to make the gripper open
            self.send_joint_position_target()
            return
        elif data_name == "Operating_Mode":
            print("[K1] operating mode:", values)
            pass
        elif data_name == "Position_P_Gain":
            print("[K1] position p gain:", values)
            pass
        elif data_name == "Position_D_Gain":
            print("[K1] position d gain:", values)
            pass
        elif data_name == "Position_I_Gain":
            print("[K1] position i gain:", values)
            pass
        else:
            raise ValueError(f"Unknown data name: {data_name}")

    def set_calibration(self, calibration: dict[str, list]):
        print("setting calibration:")
        print(calibration)
        self.calibration = calibration
    
    def apply_calibration_autocorrect(self, values: np.ndarray, motor_names: list[str] | None = None) -> np.ndarray:
        values = self.apply_calibration(values, motor_names)
        return values

    def apply_calibration(self, values: np.ndarray, motor_names: list[str] | None = None) -> np.ndarray:
        values = values.astype(np.float32)

        if motor_names is None:
            motor_names = self.motor_names

        for i, name in enumerate(motor_names):
            calib_idx = self.calibration["motor_names"].index(name)
            calib_mode = self.calibration["calib_mode"][calib_idx]

            drive_mode = self.calibration["drive_mode"][calib_idx]
            homing_offset = self.calibration["homing_offset"][calib_idx]
            resolution = MOTOR_RESOLUTION
            
            # Update direction of rotation of the motor to match between leader and follower.
            # In fact, the motor of the leader for a given joint can be assembled in an
            # opposite direction in term of rotation than the motor of the follower on the same joint.
            if drive_mode:
                values[i] *= -1

            # Convert from range [-2**31, 2**31] to
            # nominal range [-resolution//2, resolution//2] (e.g. [-2048, 2048])
            values[i] += homing_offset

            # Convert from range [-resolution//2, resolution//2] to
            # universal float32 centered degree range [-180, 180]
            # (e.g. 2048 / (4096 // 2) * 180 = 180)
            values[i] = values[i] / (resolution // 2) * HALF_TURN_DEGREE


        return values

    def revert_calibration(self, values: np.ndarray, motor_names: list[str] | None = None) -> np.ndarray:
        if motor_names is None:
            motor_names = self.motor_names

        for i, name in enumerate(motor_names):
            calib_idx = self.calibration["motor_names"].index(name)
            calib_mode = self.calibration["calib_mode"][calib_idx]


            drive_mode = self.calibration["drive_mode"][calib_idx]
            homing_offset = self.calibration["homing_offset"][calib_idx]
            resolution = MOTOR_RESOLUTION

            # Convert from nominal 0-centered degree range [-180, 180] to
            # 0-centered resolution range (e.g. [-2048, 2048] for resolution=4096)
            values[i] = values[i] / HALF_TURN_DEGREE * (resolution // 2)

            # Substract the homing offsets to come back to actual motor range of values
            # which can be arbitrary.
            values[i] -= homing_offset

            # Remove drive mode, which is the rotation direction of the motor, to come back to
            # actual motor rotation direction which can be arbitrary.
            if drive_mode:
                values[i] *= -1

        values = np.round(values).astype(np.int32)
        return values