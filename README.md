# robot_arm_vla_ros2

Software for the Unitree K1 (Sagittarius) arm: a Python SDK, a ROS 2 driver, and a web page that lets you control the arm remotely and watch its wrist camera.

| Part | Path | What it does |
|---|---|---|
| Python SDK | `src/unitree_k1_sdk/` | Talks to the arm over serial. Use it directly from scripts (`examples/`). |
| ROS 2 driver | `ros2/k1_driver_py/` | Exposes the arm as ROS topics and services. See [its README](ros2/k1_driver_py/README.md) and [COMMANDS.md](ros2/k1_driver_py/COMMANDS.md). |
| Web control | `ros2/k1_web/` | Browser UI with joint sliders, saved poses and a live camera feed, reachable from anywhere through Tailscale. See [its README](ros2/k1_web/README.md). |

All of it runs on the Linux PC the arm and camera are plugged into: Ubuntu 24.04 + ROS 2 Jazzy. Ubuntu 22.04 + Humble also works; replace `jazzy` with `humble` below and leave out `--break-system-packages`. You only need a browser on the device you control it from.

## Setup (once, on the Linux PC)

The code can live in any folder. These instructions keep the repo in `~/Downloads/robot_arm_vla_ros2`, and build it in `~/ros2_ws`, where colcon puts its `build/`, `install/` and `log/` folders. If your clone is somewhere else, change the paths to match.

### 1. Get the code

```bash
cd ~/Downloads
git clone https://github.com/eric-juu/robot_arm_vla_ros2.git
```

Already have it? Check that it's a git clone and update it: `cd ~/Downloads/robot_arm_vla_ros2 && git remote -v && git pull`. A folder downloaded as a zip (named `robot_arm_vla_ros2-main`) can't `git pull`, so use a real clone.

### 2. Install the system packages

The `python3-*` web packages are in Ubuntu's **universe** repository, so enable it first:

```bash
sudo add-apt-repository universe
sudo apt update
sudo apt install python3-serial python3-numpy python3-tqdm \
                 python3-fastapi python3-uvicorn python3-websockets python3-yaml \
                 ros-jazzy-v4l2-camera ros-jazzy-image-transport-plugins v4l-utils
```

If apt still says `Unable to locate package` for any of the `python3-*` packages, leave those out of the apt command and install them with pip instead. ROS uses the system `python3`, so it sees them:

```bash
pip install --user --break-system-packages fastapi uvicorn websockets pyyaml
```

Check:

```bash
python3 -c "import fastapi, uvicorn, websockets, yaml; print('ok')"
```

### 3. Install the SDK

```bash
pip install --no-deps -e ~/Downloads/robot_arm_vla_ros2 --break-system-packages
python3 -c "import unitree_k1_sdk, os; print(os.path.dirname(unitree_k1_sdk.__file__))"   # should point into the repo
```

### 4. Allow access to the serial port and camera

```bash
sudo usermod -aG dialout,video $USER
```

Then log out and back in.

### 5. Build

```bash
source /opt/ros/jazzy/setup.bash
mkdir -p ~/ros2_ws && cd ~/ros2_ws
colcon build --base-paths ~/Downloads/robot_arm_vla_ros2/ros2
```

It should end with `Summary: 2 packages finished` (`k1_driver_py` and `k1_web`).

### 6. Load ROS and the workspace in every terminal

ROS only finds these packages in a terminal that has sourced the workspace. Without it you get `Package 'k1_web' not found, searching: ['/opt/ros/jazzy']`. Add the two lines to `~/.bashrc` so every new terminal does it:

```bash
echo 'source /opt/ros/jazzy/setup.bash' >> ~/.bashrc
echo 'source ~/ros2_ws/install/setup.bash' >> ~/.bashrc
source ~/.bashrc
ros2 pkg list | grep k1      # expect k1_driver_py and k1_web
```

### Updating later

```bash
cd ~/Downloads/robot_arm_vla_ros2 && git pull
cd ~/ros2_ws && colcon build --base-paths ~/Downloads/robot_arm_vla_ros2/ros2
source ~/ros2_ws/install/setup.bash     # or open a new terminal
```

Restart `ros2 launch` afterwards so the new code is used.

## Controlling the arm from a browser

### 1. Pick a password token (once)

```bash
mkdir -p ~/.k1_web
echo "export K1_WEB_TOKEN=$(openssl rand -hex 16)" > ~/.k1_web/token.sh
chmod 600 ~/.k1_web/token.sh
```

Print the token:

```bash
source ~/.k1_web/token.sh && echo $K1_WEB_TOKEN
```

That prints only the 32 hex characters, which is what you type on the login page. Don't type the `export K1_WEB_TOKEN=` part. The token stays the same until you rewrite `token.sh`. If you do rewrite it, restart `ros2 launch`, because the server keeps the token it was started with.

### 2. Start everything

```bash
source ~/.k1_web/token.sh

# try it without hardware first: fake arm, no camera
ros2 launch k1_web k1_bringup.launch.py mock:=true use_camera:=false

# the real arm and camera
ros2 launch k1_web k1_bringup.launch.py port:=/dev/ttyACM0
```

This starts the arm driver (or the fake arm), the camera, and the web server. Leave the terminal open; Ctrl+C stops everything. Other variations:

```bash
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
| `Package 'k1_web' not found, searching: ['/opt/ros/jazzy']` | The workspace isn't sourced in this terminal. Run `source ~/ros2_ws/install/setup.bash`, or do setup step 6. If `ros2 pkg list \| grep k1` still lacks `k1_web`, `git pull` and rebuild. |
| `cannot access …/ros2` or colcon finds 0 packages | `--base-paths` must point at the `ros2/` folder inside your clone. Check with `ls ~/Downloads/robot_arm_vla_ros2/ros2`. |
| apt: `Unable to locate package python3-fastapi` (or similar) | Enable universe (setup step 2), or install those packages with pip |
| Login says "Wrong token" | Type only the hex part, not `export K1_WEB_TOKEN=`. Run `echo $K1_WEB_TOKEN` in the terminal that ran `ros2 launch`; the server keeps the token it was started with. |
| `K1_WEB_TOKEN is not set` | `source ~/.k1_web/token.sh` in the same terminal before launching |
| Page says "arm silent" | Driver not running or wrong `port:=`. Run `ros2 topic hz /joint_states` (expect ~50 Hz). |
| Sliders stay greyed out | Click **Enable motors** first, then wait for "enabled" in the header |
| "camera unavailable" | Run `ros2 topic hz /image_raw/compressed`, check `video_device:=`, and check that you're in the `video` group |
| `Permission denied: /dev/ttyACM0` | You're not in the `dialout` group yet (log out and back in after `usermod`) |
| Port 8080 already in use | Another copy is still running. Stop it, or change `port` in `k1_web.yaml`. |
