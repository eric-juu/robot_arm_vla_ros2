# Setup

Everything here happens once, on the Linux PC the arm and camera are plugged into (Ubuntu 24.04 + ROS 2 Jazzy; on 22.04 + Humble, replace `jazzy` with `humble` and leave out `--break-system-packages`).

## 1. Get the code and the packages

```bash
mkdir -p ~/ros2_ws/src && cd ~/ros2_ws/src
git clone https://github.com/eric-juu/robot_arm_vla_ros2.git

sudo add-apt-repository universe && sudo apt update
sudo apt install python3-serial python3-numpy python3-tqdm \
                 python3-fastapi python3-uvicorn python3-websockets python3-yaml python3-pil \
                 ros-jazzy-v4l2-camera ros-jazzy-image-transport-plugins v4l-utils \
                 ros-jazzy-xacro ros-jazzy-robot-state-publisher ros-jazzy-control-msgs \
                 ros-jazzy-rviz2 ros-jazzy-joint-state-publisher-gui
```

If apt can't find a `python3-*` package, install it with pip instead (ROS uses the system Python, so it sees it): `pip install --user --break-system-packages fastapi uvicorn websockets pyyaml`.

For remote access, install `cloudflared` (no account needed):

```bash
curl -L -o /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i /tmp/cloudflared.deb       # on an ARM PC: cloudflared-linux-arm64.deb
```

## 2. Permissions, build, and the shell

```bash
sudo usermod -aG dialout,video $USER          # serial port and camera; log out and back in afterwards

source /opt/ros/jazzy/setup.bash
cd ~/ros2_ws && colcon build                  # ends with "Summary: 5 packages finished"

echo 'source /opt/ros/jazzy/setup.bash' >> ~/.bashrc
echo 'source ~/ros2_ws/install/setup.bash' >> ~/.bashrc
```

Already have a clone elsewhere (for example `~/Downloads/robot_arm_vla_ros2`)? Link it into the workspace instead of cloning again: `ln -s ~/Downloads/robot_arm_vla_ros2 ~/ros2_ws/src/`.

## 3. The login token

```bash
mkdir -p ~/.k1_web
echo "export K1_WEB_TOKEN=$(openssl rand -hex 16)" > ~/.k1_web/token.sh
chmod 600 ~/.k1_web/token.sh
source ~/.k1_web/token.sh && echo $K1_WEB_TOKEN       # the 32 hex characters you type on the login page
```

The server keeps the token it started with: restart `ros2 launch` after changing it. Optional extras in the same file:

- `export K1_WEB_AGENT_TOKEN=$(openssl rand -hex 16)`: a separate token for AI agents. Connections with it are always agents and can't act as a person (see [safety.md](safety.md)).
- `export K1_WEB_ADMIN_PASSWORD=<something>`: an easier second password for testing. Anyone with the link can try it, so remove it when you're done.

## 4. Start and check

```bash
source ~/.k1_web/token.sh
ros2 launch k1_web k1_bringup.launch.py mock:=true use_camera:=false     # first, without hardware
ros2 launch k1_web k1_bringup.launch.py port:=/dev/ttyACM0 tunnel:=true # the real arm, camera and public link
```

Open `http://127.0.0.1:8080` on the PC, or the printed `https://….trycloudflare.com` link anywhere. Other launch arguments are in the [k1_web README](../ros2/k1_web/README.md#launch-arguments). Don't know the port? `ls /dev/ttyACM* /dev/serial/by-path/`; the camera: `v4l2-ctl --list-devices`.

## Updating

```bash
cd ~/ros2_ws/src/robot_arm_vla_ros2 && git pull
cd ~/ros2_ws && colcon build && source install/setup.bash
```

Then restart `ros2 launch`. Code is edited anywhere and pushed; only the Linux PC needs ROS.

## First run on the real arm

1. **Check the model against the arm** (`rviz:=true`). At all zeros the upper arm is vertical and the forearm horizontal, pointing forward. Move each joint +0.2 rad from the page: the RViz model must turn the same way. If either is off, the URDF needs offsets or sign flips before you trust `/tf`.
2. **Set real joint limits.** The defaults (±1.0 rad on every joint) are placeholders. With someone at the arm, move each joint slowly to find its safe range, and write it into `ros2/k1_web/config/k1_web.yaml` (as floats), then rebuild or pass your copy with `web_params_file:=`. The URDF's hardware ranges are the outer bound.
3. **Save a `home` pose** from the page.
4. **Decide what agents may do** in the same file (`agent_*` parameters, see [safety.md](safety.md)): for example tighter joint bounds or a lower speed for agents, and `agent_free_motion: false` once the bench only needs taught poses.
5. **Calibrate what's still assumed** (see the [k1_description README](../ros2/k1_description/README.md#assumptions-to-verify)): the gripper's metres scale, and the wrist camera's image directions (record them in `agent/skills/wrist-camera.md`).

## Remote access

`tunnel:=true` starts a Cloudflare quick tunnel and prints its link. The link is public and new on every restart; the token is what protects the arm (wrong logins are slowed to one per second, and the session cookie is HTTPS-only through the tunnel). Quick tunnels have no uptime guarantee. For a lab:

- **A named Cloudflare tunnel** gives a fixed URL (needs a free account and a domain): `cloudflared tunnel login`, `cloudflared tunnel create k1`, a DNS route. Put **Cloudflare Access** (email login) in front once other people need access.
- **Tailscale** (`tailscale serve --bg 8080`) keeps it private, but every device needs the app.
- `ros2 run k1_web tunnel` starts the tunnel alone, for example while the launch already runs.

### Start on boot

Put `K1_WEB_TOKEN=<token>` (and optionally `K1_WEB_AGENT_TOKEN=…`) in `~/.k1_web/env` (`chmod 600`), then create `~/.config/systemd/user/k1.service`:

```ini
[Unit]
Description=K1 arm bringup (driver, camera, web)
After=network-online.target

[Service]
EnvironmentFile=%h/.k1_web/env
ExecStart=/bin/bash -c 'source /opt/ros/jazzy/setup.bash && source %h/ros2_ws/install/setup.bash && exec ros2 launch k1_web k1_bringup.launch.py tunnel:=true'
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now k1.service
sudo loginctl enable-linger $USER          # start at boot without logging in
journalctl --user -u k1.service | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1    # the current link
```

The motors stay disabled at startup unless the driver's `enable_on_start` is set.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Package 'k1_web' not found` | The workspace isn't sourced in this terminal: `source ~/ros2_ws/install/setup.bash` (or the `.bashrc` lines above). Still missing from `ros2 pkg list \| grep k1`? `git pull` and rebuild. |
| colcon finds 0 packages | The repo must be inside `~/ros2_ws/src` (or linked there). |
| `No module named 'unitree_k1_sdk'`, xacro or robot_state_publisher errors | Install the apt packages above, rebuild, source. |
| apt: `Unable to locate package python3-fastapi` | Enable universe, or use the pip line above. |
| `K1_WEB_TOKEN is not set` | `source ~/.k1_web/token.sh` in the terminal that launches. |
| Login says "Wrong token" | Type only the hex part. The server keeps the token it started with: `echo $K1_WEB_TOKEN` in that terminal. |
| Page says "arm silent" | Driver not running or wrong `port:=`: `ros2 topic hz /joint_states` should show ~50 Hz. |
| `Permission denied: /dev/ttyACM0` | Not in `dialout` yet: log out and back in after `usermod`. |
| Sliders stay greyed out | Click **Enable motors** and wait for "enabled" in the header. |
| "camera unavailable" | `ros2 topic hz /image_raw/compressed`; check `video_device:=` and the `video` group. |
| `cloudflared is not installed` | See step 1. |
| Tunnel link shows a Cloudflare error | Wait ~30 s and reload; check `curl -I http://127.0.0.1:8080`. |
| No link appears, or `[tunnel] … ERR` lines | The network may block Cloudflare's default protocol: try `cloudflared tunnel --protocol http2 --url http://127.0.0.1:8080`. |
| Port 8080 already in use | Another copy is running; stop it, or change `port` in `k1_web.yaml`. |
| A ROS goal fails with `motors are disabled` or `outside k1_web's joint limits` | Enable the motors on the page, or stay inside the limits in `k1_web.yaml`. |
| An agent's moves fail with `k1_web refuses …` | That's k1_web's agent policy ([safety.md](safety.md)); change the `agent_*` parameters if it should be allowed. |

Agent problems (keys, sign-in, `no reply to 'hello'`) are in the [agent README](../agent/README.md#troubleshooting).
