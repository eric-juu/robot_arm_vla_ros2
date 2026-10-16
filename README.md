# robot_arm_vla_ros2

Software for the Unitree K1 (Sagittarius) arm: a Python SDK, a ROS 2 driver, and a web page that lets you control the arm remotely and watch its wrist camera.

| Part | Path | What it does |
|---|---|---|
| Python SDK | `src/unitree_k1_sdk/` | Talks to the arm over serial. Use it directly from scripts (`examples/`). |
| ROS 2 driver | `ros2/k1_driver_py/` | Exposes the arm as ROS topics and services. See [its README](ros2/k1_driver_py/README.md) and [COMMANDS.md](ros2/k1_driver_py/COMMANDS.md). |
| Web control | `ros2/k1_web/` | Browser UI with joint sliders, saved poses and a live camera feed, reachable from anywhere through Tailscale. See [its README](ros2/k1_web/README.md). |

All of it runs on the Linux PC the arm and camera are plugged into (Ubuntu 22.04 + ROS 2 Humble, or 24.04 + Jazzy). You only need a browser on the device you control it from.

## Setup (once, on the Linux PC)

```bash
# 1. get the code into a ROS 2 workspace
mkdir -p ~/ros2_ws/src && cd ~/ros2_ws/src
git clone https://github.com/eric-juu/robot_arm_vla_ros2.git

# 2. dependencies
sudo apt install python3-serial python3-numpy python3-tqdm \
                 python3-fastapi python3-uvicorn python3-websockets python3-yaml \
                 ros-$ROS_DISTRO-v4l2-camera ros-$ROS_DISTRO-image-transport-plugins
pip install --no-deps -e ~/ros2_ws/src/robot_arm_vla_ros2   # add --break-system-packages on Ubuntu 24.04

# 3. serial port and camera access (log out and back in afterwards)
sudo usermod -aG dialout,video $USER

# 4. build
source /opt/ros/$ROS_DISTRO/setup.bash
cd ~/ros2_ws
colcon build --base-paths src/robot_arm_vla_ros2/ros2
```

To update later, pull and rebuild: `cd ~/ros2_ws/src/robot_arm_vla_ros2 && git pull && cd ~/ros2_ws && colcon build --base-paths src/robot_arm_vla_ros2/ros2`.

## Controlling the arm from a browser

### 1. Pick a password token (once)

```bash
mkdir -p ~/.k1_web
echo "export K1_WEB_TOKEN=$(openssl rand -hex 16)" > ~/.k1_web/token.sh
chmod 600 ~/.k1_web/token.sh
cat ~/.k1_web/token.sh      # this is what you type on the login page
```

### 2. Start everything

```bash
source /opt/ros/$ROS_DISTRO/setup.bash
source ~/ros2_ws/install/setup.bash
source ~/.k1_web/token.sh

ros2 launch k1_web k1_bringup.launch.py port:=/dev/ttyACM0
```

This starts the arm driver, the camera, and the web server. Useful variations:

```bash
ros2 launch k1_web k1_bringup.launch.py mock:=true use_camera:=false   # no hardware: fake arm, no camera
ros2 launch k1_web k1_bringup.launch.py use_camera:=false              # arm only
ros2 launch k1_web k1_bringup.launch.py video_device:=/dev/video2      # a different camera
```

If you don't know the arm's port, check `ls /dev/ttyACM* /dev/serial/by-path/`. For the camera, check `v4l2-ctl --list-devices`.

### 3. Open the page

- **On the Linux PC itself:** `http://127.0.0.1:8080`
- **From anywhere else:** set up [Tailscale](https://tailscale.com) once:
  1. Install it on the PC and on your laptop/phone, and sign in to the same account on both.
  2. On the PC, run `sudo tailscale up`, then `sudo tailscale serve --bg 8080`.
  3. Open `https://<pc-name>.<tailnet>.ts.net`. The exact URL is printed by `tailscale serve status`.

Sign in with the token.

### 4. Drive the arm

1. Click **Enable motors**. The arm locks in place, and the sliders unlock once its position has been read.
2. Drag a joint slider. The arm moves there smoothly at a capped speed, and the number next to each slider is the measured angle in radians.
3. Use the **gripper** slider to open (1) and close (0) the gripper.
4. Click a **pose** button to go to a saved pose. **Save current pose…** stores where the arm is now.
5. **STOP**, or the Space / Esc key, holds the arm where it is.
6. **Disable motors** releases the arm. **It goes limp and falls, so support it first.**

If you close the tab or lose your connection, the arm stops and holds its position within about a second.

> **First time on the real arm:** joints are limited to ±1.0 rad until you set real limits. With someone standing at the arm, find each joint's safe range and write it into `ros2/k1_web/config/k1_web.yaml` (see the [k1_web README](ros2/k1_web/README.md#first-run-on-the-real-arm)). Then save a `home` pose from the page.

## Using the camera in ROS

While the camera node runs, frames are published on `/image_raw` and `/image_raw/compressed` (JPEG):

```bash
ros2 topic hz /image_raw/compressed
ros2 bag record /image_raw/compressed /joint_states /joint_position_command /gripper_command -o my_run
```

The web server also serves `/snapshot.jpg` (a single frame) and `/video.mjpeg` (a stream) to logged-in clients.

## Scripts and the future AI agent

There are three ways to drive the arm from code, from highest to lowest level:

- **Web API:** connect to `ws://…/ws` with `Authorization: Bearer $K1_WEB_TOKEN` and send JSON goals. This gets the same limits, smoothing and stop-on-disconnect as the page. Protocol and a Python example are in the [k1_web README](ros2/k1_web/README.md#api-for-scripts-and-the-future-agent).
- **ROS topics:** publish to `/joint_position_command` and `/gripper_command` directly (see [COMMANDS.md](ros2/k1_driver_py/COMMANDS.md)). There are no limits or smoothing at this level, so use small steps.
- **Python SDK, without ROS:** `python examples/demo.py /dev/ttyACM0`. Stop the ROS driver first, because only one program can own the serial port.

## Tests

```bash
cd ros2/k1_web && python -m pytest test
```

## Troubleshooting

| Symptom | Check |
|---|---|
| `K1_WEB_TOKEN is not set` | `source ~/.k1_web/token.sh` in the same terminal before launching |
| Page says "arm silent" | Driver not running or wrong `port:=`. Run `ros2 topic hz /joint_states` (expect ~50 Hz). |
| Sliders stay greyed out | Click **Enable motors** first, then wait for "enabled" in the header |
| "camera unavailable" | Run `ros2 topic hz /image_raw/compressed`, check `video_device:=`, and check that you're in the `video` group |
| `Permission denied: /dev/ttyACM0` | You're not in the `dialout` group yet (log out and back in after `usermod`) |
| Port 8080 already in use | Another copy is still running. Stop it, or change `port` in `k1_web.yaml`. |
