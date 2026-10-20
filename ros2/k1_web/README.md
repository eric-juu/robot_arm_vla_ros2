# k1_web

A web page for controlling the K1 arm remotely, with the wrist camera's live feed. It runs on the Linux PC next to `k1_driver`, and you open it from anywhere through a Cloudflare tunnel.

```
Browser ──https──> Cloudflare ──tunnel──> Linux PC
                                          ├─ tunnel      (cloudflared, started with tunnel:=true)
                                          ├─ k1_driver   (k1_driver_py, unchanged)
                                          ├─ camera      (v4l2_camera) → /image_raw, /image_raw/compressed
                                          └─ k1_web      web server + ROS node, on 127.0.0.1:8080
```

`cloudflared` makes an outgoing connection to Cloudflare, so the PC needs no open ports or router changes.

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

# real arm + camera + web + public link
ros2 launch k1_web k1_bringup.launch.py port:=/dev/ttyACM0 tunnel:=true
```

Then open `http://127.0.0.1:8080` on the PC, or the `https://….trycloudflare.com` link printed in the launch output from anywhere, and sign in with the token. The server keeps the token it was started with, so restart the launch if you change it.

Launch arguments (`--show-args` lists them):

| Argument | Default | Meaning |
|---|---|---|
| `port` | `/dev/ttyACM0` | arm serial port |
| `mock` | `false` | use the fake arm instead of `k1_driver` |
| `use_camera` | `true` | start the camera node |
| `video_device` | `/dev/video0` | camera device |
| `tunnel` | `false` | start a Cloudflare quick tunnel and print its public link |
| `web_port` | `8080` | port of the web server (and the tunnel's target) |
| `web_params_file` | `config/k1_web.yaml` | |
| `camera_params_file` | `config/camera.yaml` | |

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

## Remote access

### Cloudflare quick tunnel (current setup)

Install `cloudflared` once (top-level README, setup step 2), then add `tunnel:=true` to the launch command. You can also run the tunnel on its own with `ros2 run k1_web tunnel [http://127.0.0.1:8080]`. Either way it runs `cloudflared tunnel --url http://127.0.0.1:8080` and prints the public `https://<random-words>.trycloudflare.com` link in a box.

- **No account or client app** is needed; the link works in any browser.
- **The link changes on every restart.**
- **The link is public**, so the token is the only protection. Keep it long (the 32 hex characters from `openssl rand -hex 16`) and private. Wrong logins are slowed to one try per second.
- **The session cookie is set HTTPS-only automatically** when you sign in through the tunnel. Uvicorn takes `X-Forwarded-Proto` from the local `cloudflared`, so plain `http://127.0.0.1:8080` still works on the PC. `secure_cookie: true` forces HTTPS-only everywhere.
- **Quick tunnels are meant for testing:** they come with no uptime guarantee.

### Later: a permanent link

A **named Cloudflare tunnel** gives a fixed URL such as `https://arm.example.com`. It needs:
- a free Cloudflare account;
- a domain on Cloudflare (~$10/yr);
- `cloudflared tunnel login`, `cloudflared tunnel create k1`, and a DNS route.

You can also put **Cloudflare Access** (email login) in front of it, so strangers never even reach the token page. That's the right setup once other people need access.

**Tailscale** (`tailscale serve --bg 8080`) is the private alternative: nothing is public, but every device needs the Tailscale app.

### Start on boot (systemd user service)

Put `K1_WEB_TOKEN=<token>` in `~/.k1_web/env` (`chmod 600`), then create `~/.config/systemd/user/k1.service`:

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
sudo loginctl enable-linger $USER     # start at boot without logging in
journalctl --user -u k1.service -f
journalctl --user -u k1.service | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1   # current public link
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
| `{"type":"hello","role":"agent"}` | say who you are (`human` is the default). The page shows a banner while an agent is connected |
| `{"type":"agent_note","text":"..."}` | show a line in every page's Agent log |

Add `"id": <any>` to a message to get exactly one reply carrying that id: `ack`, `error`, `enable_result` or `info`.

The server pushes these messages:
- `{"type":"state", names, positions, stale, enabled, ready, goal, commanded, gripper, limits_min, limits_max, poses, clients}` at `state_rate_hz`. `clients` counts connections by role.
- `{"type":"error","message":...}` when a command is rejected.
- `{"type":"event","name":"stop","by":role}` to everyone when someone presses STOP. The agent aborts its task when a human does.

The [agent package](../../agent/README.md) wraps all of this in a blocking client (`k1_agent.client.K1Client`).

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
