# k1_web

A web page for controlling the K1 arm remotely, with the wrist camera's live feed. It runs on the Linux PC next to `k1_driver`, and you open it from anywhere through Tailscale.

```
Browser ──https──> Tailscale ──> Linux PC
                                 ├─ k1_driver   (k1_driver_py, unchanged)
                                 ├─ camera      (v4l2_camera) → /image_raw, /image_raw/compressed
                                 └─ k1_web      web server + ROS node
```

The browser only ever sends *goals*. The `k1_web` node enforces safety on the server side:

- **Joint limits:** every goal is clamped to `joint_limits_min/max`.
- **Smoothing:** the commanded pose moves toward the goal at no more than `max_joint_velocity` and is sent to the driver at `control_rate_hz`. Keep the driver's `command_duration_ms` at `0`.
- **No jump on enable:** after the motors are enabled, the command starts from the arm's measured pose.
- **Deadman:** if the page disconnects or goes quiet for `deadman_timeout_sec`, the arm holds where it is. The motors are never released automatically.
- **Stale state:** if `joint_states` is older than `state_timeout_sec`, the node stops sending commands.
- **Auth:** a token from the `K1_WEB_TOKEN` environment variable. The server listens on `127.0.0.1` only.

## Install

Follow the setup steps in the [top-level README](../../README.md#setup-once-on-the-linux-pc). They cover:
- the apt packages, including enabling universe, with a pip fallback;
- installing the SDK;
- the `dialout`/`video` groups;
- building with `colcon build --base-paths ~/Downloads/robot_arm_vla_ros2/ros2`;
- sourcing `~/ros2_ws/install/setup.bash` in every terminal.

## Run

```bash
source ~/.k1_web/token.sh       # exports K1_WEB_TOKEN; created in the top-level README
echo $K1_WEB_TOKEN              # the login token: only these 32 hex characters

# no hardware: fake arm, no camera
ros2 launch k1_web k1_bringup.launch.py mock:=true use_camera:=false

# real arm + camera + web
ros2 launch k1_web k1_bringup.launch.py port:=/dev/ttyACM0
```

Then open `http://127.0.0.1:8080` on the PC, or the Tailscale URL (below) from anywhere, and sign in with the token. The server keeps the token it was started with, so restart the launch if you change it.

Launch arguments: `port`, `mock`, `use_camera`, `video_device`, `web_params_file`, `camera_params_file` (`--show-args` lists them).

### Using the page

1. **Enable motors.** The sliders unlock once the arm's state has arrived.
2. Drag a joint slider. The arm follows at `max_joint_velocity`. The number next to each slider is the measured position.
3. **STOP** (or Space / Esc) holds the current position.
4. Pose buttons move to a named pose. **Save current pose…** stores the measured pose in `~/.k1_web/poses.yaml`.
5. **Disable motors** asks for confirmation, because the arm goes limp.

### First run on the real arm

The default limits (±1.0 rad on every joint) are placeholders. With someone standing at the arm, move each joint slowly to find its safe range. Write the ranges into `config/k1_web.yaml` as floats, then rebuild, or pass your own copy with `web_params_file:=`. Save a `home` pose from the UI.

## Camera

`v4l2_camera` publishes `/image_raw` (raw) and `/image_raw/compressed` (JPEG). The web page uses the compressed topic, and any other ROS node can subscribe to either one. For example, to record a dataset:

```bash
ros2 bag record /image_raw/compressed /joint_states /joint_position_command /gripper_command
```

To check the camera: `v4l2-ctl --list-devices`, then `ros2 topic hz /image_raw/compressed`. Device, resolution and JPEG quality are set in `config/camera.yaml`.

## Remote access with Tailscale

1. Install Tailscale on the Linux PC and on your laptop/phone, and sign in to the same account on both (`curl -fsSL https://tailscale.com/install.sh | sh`, then `sudo tailscale up`).
2. On the PC, run `sudo tailscale serve --bg 8080`.
3. Open `https://<pc-name>.<tailnet>.ts.net` from any of your devices.

Only devices on your tailnet can reach the page, and Tailscale provides the HTTPS. Once you only use it over https, set `secure_cookie: true`. To let people in who don't have Tailscale, `tailscale funnel` makes the page public. The token is then the only thing protecting the arm, so use a long one.

### Start on boot (systemd user service)

Put `K1_WEB_TOKEN=<token>` in `~/.k1_web/env` (`chmod 600`), then create `~/.config/systemd/user/k1.service`:

```ini
[Unit]
Description=K1 arm bringup (driver, camera, web)
After=network-online.target

[Service]
EnvironmentFile=%h/.k1_web/env
ExecStart=/bin/bash -c 'source /opt/ros/jazzy/setup.bash && source %h/ros2_ws/install/setup.bash && exec ros2 launch k1_web k1_bringup.launch.py'
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now k1.service
sudo loginctl enable-linger $USER     # start at boot without logging in
journalctl --user -u k1.service -f
```

The motors stay disabled at startup unless `enable_on_start` is set for the driver.

## API (for scripts and the future agent)

Authenticate with the session cookie or an `Authorization: Bearer <token>` header.

- `GET /snapshot.jpg`: the latest camera frame
- `GET /video.mjpeg`: an MJPEG stream
- `WS /ws`: JSON messages

Messages you can send on `/ws`. Any message also counts as a heartbeat; send at least one per second while moving.

| Message | Effect |
|---|---|
| `{"type":"ping"}` | heartbeat only |
| `{"type":"enable","value":true}` | lock (`true`) or release (`false`) the motors. Reply: `enable_result` |
| `{"type":"goal","positions":[6 floats]}` | arm goal in radians, clamped to the limits |
| `{"type":"gripper","value":0.0..1.0}` | 1 open, 0 closed |
| `{"type":"pose","name":"zero"}` | move to a named pose |
| `{"type":"stop"}` | hold the current position |
| `{"type":"save_pose","name":"..."}` | save the measured pose. Reply: `info` |

The server pushes `{"type":"state", names, positions, stale, enabled, ready, goal, commanded, gripper, limits_min, limits_max, poses}` at `state_rate_hz`, and `{"type":"error","message":...}` when a command is rejected.

Minimal Python client:

```python
import asyncio, json, os, websockets

async def main():
    headers = {"Authorization": f"Bearer {os.environ['K1_WEB_TOKEN']}"}
    async with websockets.connect("ws://127.0.0.1:8080/ws", additional_headers=headers) as ws:
        await ws.send(json.dumps({"type": "enable", "value": True}))
        await asyncio.sleep(0.5)
        for _ in range(40):     # keep sending for 4 s so the deadman doesn't trigger
            await ws.send(json.dumps({"type": "goal", "positions": [0.3, 0, 0, 0, 0, 0]}))
            await asyncio.sleep(0.1)

asyncio.run(main())
```

(On `websockets` < 14, use `extra_headers=` instead of `additional_headers=`.)

## Tests

```bash
colcon test --packages-select k1_web --base-paths ~/Downloads/robot_arm_vla_ros2/ros2 && colcon test-result --verbose
# or, without ROS: cd ros2/k1_web && python -m pytest test
```
