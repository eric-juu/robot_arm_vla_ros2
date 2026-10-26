# robot_arm_vla_ros2

Software for the Unitree K1 (Sagittarius) arm, as a ROS 2 workspace:
- a serial driver;
- a URDF model;
- standard trajectory and gripper actions;
- a web page that lets you control the arm remotely and watch its wrist camera;
- an AI agent.

| Part | Path | What it does |
|---|---|---|
| SDK | `ros2/unitree_k1_sdk/` | The serial protocol, as a plain Python library (a ROS package so colcon installs it). See [its README](ros2/unitree_k1_sdk/README.md). |
| ROS 2 driver | `ros2/k1_driver_py/` | Exposes the arm as ROS topics and services (`joint_states`, `enabled`, `enable`). See [its README](ros2/k1_driver_py/README.md) and [COMMANDS.md](ros2/k1_driver_py/COMMANDS.md). |
| Arm model | `ros2/k1_description/` | URDF and meshes (from NXROBO's Sagittarius). Gives `/tf` and the RViz model. See [its README](ros2/k1_description/README.md). |
| Web control and motion | `ros2/k1_web/` | Browser UI with joint sliders, saved poses and a live camera feed, reachable from anywhere through a Cloudflare tunnel. Also the safety layer every motion goes through, and the `FollowJointTrajectory` / `GripperCommand` actions for ROS tools. Holds the bringup launch file. See [its README](ros2/k1_web/README.md). |
| ROS examples | `ros2/k1_examples/` | `ros2 run k1_examples move/gripper/zero/demo`, and a small client class for your own scripts. See [its README](ros2/k1_examples/README.md). |
| AI agent | `agent/` | An LLM (Claude, or OpenAI GPT-5.x / GPT-6 Astra) that does tasks through the web API, with traces, a skill library and an MCP server. See [its README](agent/README.md). |

All of it runs on the Linux PC the arm and camera are plugged into: Ubuntu 24.04 + ROS 2 Jazzy. Ubuntu 22.04 + Humble also works; replace `jazzy` with `humble` below and leave out `--break-system-packages`. You only need a browser on the device you control it from.

## Every session

Once the [one-time setup](#setup-once-on-the-linux-pc) is done, and you've [created your token](#1-pick-a-password-token-once), this is all you do each time you use the arm.

**On the Linux PC:**

1. Power on the arm, and plug in its USB cable and the camera.
2. Open a terminal. ROS and the workspace load automatically from `~/.bashrc` (setup step 6).
3. Start everything:
   ```bash
   source ~/.k1_web/token.sh
   ros2 launch k1_web k1_bringup.launch.py port:=/dev/ttyACM0 tunnel:=true
   ```
   To test without hardware instead, use `ros2 launch k1_web k1_bringup.launch.py mock:=true use_camera:=false tunnel:=true`.
4. Wait for the boxed `https://….trycloudflare.com` link in the output. **It's a new link every session.**

**On your phone or laptop:**

5. Open the link, which can take ~30 s to start working, and sign in with your token. You have to sign in again each session, because each new link is a new site to the browser. To see the token: `source ~/.k1_web/token.sh && echo $K1_WEB_TOKEN`.
6. Click **Enable motors** and drive the arm (see [Drive the arm](#4-drive-the-arm)).

**When you're done:**

7. Move the arm to a safe resting pose, and support it if needed.
8. Click **Disable motors**. The arm goes limp.
9. Press **Ctrl+C** in the launch terminal. That stops the driver, camera, web server and tunnel, and the link stops working.

**If the code changed since last time** (someone pushed to GitHub), update before step 3:

```bash
cd ~/Downloads/robot_arm_vla_ros2 && git pull
cd ~/ros2_ws && colcon build --base-paths ~/Downloads/robot_arm_vla_ros2/ros2
source ~/ros2_ws/install/setup.bash
```

When you change the code yourself on another computer, commit and `git push` there, then run the update above on the Linux PC. Only the Linux PC needs ROS; the other computer only edits code.

Optional: add `source ~/.k1_web/token.sh` to `~/.bashrc` too (`echo 'source ~/.k1_web/token.sh' >> ~/.bashrc`), and step 3 becomes just the `ros2 launch` line.

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
                 python3-fastapi python3-uvicorn python3-websockets python3-yaml python3-pil \
                 ros-jazzy-v4l2-camera ros-jazzy-image-transport-plugins v4l-utils \
                 ros-jazzy-xacro ros-jazzy-robot-state-publisher ros-jazzy-control-msgs \
                 ros-jazzy-rviz2 ros-jazzy-joint-state-publisher-gui
```

(Most of the `ros-jazzy-*` packages come with `ros-jazzy-desktop` already. `rviz2` and `joint-state-publisher-gui` are only needed for the 3D view.)

If apt still says `Unable to locate package` for any of the `python3-*` packages, leave those out of the apt command and install them with pip instead. ROS uses the system `python3`, so it sees them:

```bash
pip install --user --break-system-packages fastapi uvicorn websockets pyyaml
```

Check:

```bash
python3 -c "import fastapi, uvicorn, websockets, yaml; print('ok')"
```

For remote access, also install `cloudflared` (Cloudflare's tunnel client, not in apt). It needs no account:

```bash
curl -L -o /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i /tmp/cloudflared.deb       # on an ARM PC use cloudflared-linux-arm64.deb
cloudflared --version
```

### 3. The SDK (nothing to do)

colcon now builds the SDK as the ROS package `unitree_k1_sdk` (step 5), so there's no pip step. If you installed it with pip before, remove that copy so there's only one:

```bash
pip uninstall --break-system-packages Unitree-K1-SDK     # "not installed" is fine
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

It should end with `Summary: 5 packages finished` (`unitree_k1_sdk`, `k1_description`, `k1_driver_py`, `k1_examples` and `k1_web`).

### 6. Load ROS and the workspace in every terminal

ROS only finds these packages in a terminal that has sourced the workspace. Without it you get `Package 'k1_web' not found, searching: ['/opt/ros/jazzy']`. Add the two lines to `~/.bashrc` so every new terminal does it:

```bash
echo 'source /opt/ros/jazzy/setup.bash' >> ~/.bashrc
echo 'source ~/ros2_ws/install/setup.bash' >> ~/.bashrc
source ~/.bashrc
ros2 pkg list | grep k1      # expect k1_description, k1_driver_py, k1_examples, k1_web and unitree_k1_sdk
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

Optional: a second, easier password for testing. It's kept on the PC only, never in the repo:

```bash
echo 'export K1_WEB_ADMIN_PASSWORD=<your-password>' >> ~/.k1_web/token.sh
```

The server then accepts it at login (and as a Bearer token) just like the token, and logs a warning at startup. Remove the line and restart `ros2 launch` to turn it off. Anyone with the tunnel link can try it, so keep it non-obvious and turn it off when you're done testing.

### 2. Start everything

```bash
source ~/.k1_web/token.sh

# try it without hardware first: fake arm, no camera
ros2 launch k1_web k1_bringup.launch.py mock:=true use_camera:=false

# the real arm and camera, reachable from anywhere
ros2 launch k1_web k1_bringup.launch.py port:=/dev/ttyACM0 tunnel:=true
```

This starts:
- the arm driver (or the fake arm);
- `robot_state_publisher` with the arm model;
- the camera;
- the web server;
- with `tunnel:=true`, the public link.

Leave the terminal open; Ctrl+C stops everything. Other variations:

```bash
ros2 launch k1_web k1_bringup.launch.py use_camera:=false              # arm only
ros2 launch k1_web k1_bringup.launch.py video_device:=/dev/video2      # a different camera
ros2 launch k1_web k1_bringup.launch.py rviz:=true                     # also a 3D view of the arm (on the PC's desktop)
```

If you don't know the arm's port, check `ls /dev/ttyACM* /dev/serial/by-path/`. For the camera, check `v4l2-ctl --list-devices`.

### 3. Open the page

- **On the Linux PC itself:** `http://127.0.0.1:8080`
- **From anywhere else** (phone, laptop, any network): launch with `tunnel:=true` and look for this box in the launch output:
  ```
  ====================================================
    https://some-random-words.trycloudflare.com
  ====================================================
  ```
  Open that link in any browser. Nothing needs to be installed on that device. The link can take up to ~30 s to start working.

Sign in with the token.

About the link:
- **It changes every time you restart the launch.** Get the new one from the output each time.
- **It's public:** anyone who has it reaches the login page, so the token is what protects the arm. Don't share the token or post the link.
- **It's a Cloudflare "quick tunnel":** free, no account, meant for testing, and with no uptime guarantee. For a permanent link, set up a named Cloudflare tunnel with your own domain (see the [k1_web README](ros2/k1_web/README.md#remote-access)).

To run the tunnel without the launch file (for example while the launch is already running): `ros2 run k1_web tunnel`.

### 4. Drive the arm

1. Click **Enable motors**. The arm locks in place, and the sliders unlock once its position has been read.
2. Drag a joint slider. The arm moves there smoothly at a capped speed, and the number next to each slider is the measured angle in radians.
3. Use the **gripper** slider to open (1) and close (0) the gripper.
4. Click a **pose** button to go to a saved pose. **Save current pose…** stores where the arm is now.
5. **STOP**, or the Space / Esc key, holds the arm where it is.
6. **Disable motors** releases the arm. **It goes limp and falls, so support it first.**

If you close the tab or lose your connection, the arm stops and holds its position within about a second.

> **First time on the real arm:** check that the RViz model (`rviz:=true`) matches the real arm's zero pose and joint directions (see the [k1_web README](ros2/k1_web/README.md#first-run-on-the-real-arm)). Joints are limited to ±1.0 rad until you set real limits. With someone standing at the arm, find each joint's safe range and write it into `ros2/k1_web/config/k1_web.yaml` (see the [k1_web README](ros2/k1_web/README.md#first-run-on-the-real-arm)). Then save a `home` pose from the page.

## Using the camera in ROS

While the camera node runs, frames are published on `/image_raw` and `/image_raw/compressed` (JPEG):

```bash
ros2 topic hz /image_raw/compressed
ros2 bag record /image_raw/compressed /joint_states /tf /joint_position_command /gripper_command -o my_run
```

The web server also serves `/snapshot.jpg` (a single frame) and `/video.mjpeg` (a stream) to logged-in clients.

## AI agent and scripts

The **AI agent** (`agent/`) takes a task in plain words ("pick up the red block") and does it through the web API. It sees the wrist camera and shows its steps in the page's Agent log; STOP on the page aborts it. Setup and usage are in the [agent README](agent/README.md). In short:

```bash
k1-agent observe                                    # connection check
k1-agent run "turn the base left a little" --allow-motion
k1-agent login                                      # optional: use Claude Code (a Claude account) as the model
k1-agent serve --provider claude-code --allow-motion   # then type tasks into the page's Agent card
```

There are several ways to drive the arm from your own code. The first three all go through k1_web's limits, speed limit and STOP:

- **ROS actions:** `arm_controller/follow_joint_trajectory` (`control_msgs/FollowJointTrajectory`) and `gripper_controller/gripper_cmd` (`control_msgs/GripperCommand`), the standard interfaces MoveIt and other ROS tools use. Try them with `ros2 run k1_examples move joint1=0.3`, or use the `K1Arm` class in [k1_examples](ros2/k1_examples/README.md). Details are in the [k1_web README](ros2/k1_web/README.md#ros-2-interface).
- **Agent primitives:** `from k1_agent.client import K1Client` and `from k1_agent.primitives import Primitives` give blocking calls like `move_joints(...)` and `observe()` (see the agent README).
- **Web API:** connect to `ws://…/ws` with `Authorization: Bearer $K1_WEB_TOKEN` and send JSON goals. Protocol and a Python example are in the [k1_web README](ros2/k1_web/README.md#api-for-scripts-and-the-future-agent).
- **Raw ROS topics:** publish to `/joint_position_command` and `/gripper_command` directly (see [COMMANDS.md](ros2/k1_driver_py/COMMANDS.md)). There are no limits or smoothing at this level, so use small steps.
- **SDK, without ROS:** `python3 ros2/unitree_k1_sdk/examples/demo.py /dev/ttyACM0`. Stop the ROS driver first, because only one program can own the serial port.

The arm's pose in 3D is on `/tf`. For example, `ros2 run tf2_ros tf2_echo world link_grasping_frame` prints where the point between the fingers is.

## Tests

```bash
cd ~/ros2_ws && colcon test --base-paths ~/Downloads/robot_arm_vla_ros2/ros2 && colcon test-result --verbose
cd ros2/k1_web && python -m pytest test          # or one package without ROS
cd agent && python -m pytest tests               # needs the agent's test extras, see agent/README.md
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
| `cloudflared is not installed` | Install it (end of setup step 2) |
| Tunnel link shows a Cloudflare error page | Wait ~30 s after the link appears and reload. Check that the web server is running (`curl -I http://127.0.0.1:8080`). |
| No link ever appears, or `[tunnel] … ERR` lines | The network may block Cloudflare's default protocol. Run `cloudflared tunnel --protocol http2 --url http://127.0.0.1:8080` by hand to try the fallback. |
| Port 8080 already in use | Another copy is still running. Stop it, or change `port` in `k1_web.yaml`. |
| `Package 'k1_description' not found`, or xacro / robot_state_publisher errors at launch | Install the `ros-jazzy-xacro ros-jazzy-robot-state-publisher ros-jazzy-control-msgs` packages (setup step 2), then rebuild |
| `No module named 'unitree_k1_sdk'` | Rebuild (`colcon build --base-paths …/ros2`) and source `~/ros2_ws/install/setup.bash`. The SDK is now part of the workspace |
| A ROS goal fails with `motors are disabled` or `outside k1_web's joint limits` | Enable the motors on the page (or `--enable`), or stay inside the limits in `k1_web.yaml` |
