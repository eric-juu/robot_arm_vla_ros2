# k1_web

The web page for the K1 (joint sliders, saved poses, the live wrist camera, the agent's log and prompt box), the safety layer every motion goes through, and the standard ROS motion actions. It also holds the bringup launch file. Setup is in [docs/setup.md](../../docs/setup.md); the safety layers are described in [docs/safety.md](../../docs/safety.md).

```
Browser ──https──> Cloudflare ──tunnel──> Linux PC
                                          ├─ tunnel        (cloudflared, tunnel:=true)
                                          ├─ k1_driver     (k1_driver_py) serial ↔ /joint_states, /enabled, commands
                                          ├─ robot_state_publisher  URDF (k1_description) → /tf, /robot_description
                                          ├─ camera        (v4l2_camera) → /image_raw, /image_raw/compressed
                                          ├─ k1_web        web server + ROS node on 127.0.0.1:8080, the safety layer,
                                          │                and the trajectory / gripper actions
                                          └─ rviz2         (optional, rviz:=true)
```

`control.py` (the safety controller), `policy.py` (the agent policy), `server.py` (FastAPI) and `trajectory.py` have no ROS imports: the agent's simulator and the tests run them without ROS.

## Launch arguments

`ros2 launch k1_web k1_bringup.launch.py [args]`; `--show-args` lists them. The web node needs `K1_WEB_TOKEN` in the environment.

| Argument | Default | Meaning |
|---|---|---|
| `port` | `/dev/ttyACM0` | arm serial port |
| `mock` | `false` | a fake arm instead of `k1_driver` |
| `use_camera` | `true` | start the camera node |
| `video_device` | `/dev/video0` | camera device |
| `tunnel` | `false` | start a Cloudflare quick tunnel and print its public link |
| `web_port` | `8080` | port of the web server (and the tunnel's target) |
| `rviz` | `false` | RViz with the live arm model (needs a desktop session) |
| `mount_xyz` / `mount_rpy` | `0 0 0` | where the arm's base sits in the `world` frame (metres / radians) |
| `web_params_file` | `config/k1_web.yaml` | |
| `camera_params_file` | `config/camera.yaml` | device, resolution, JPEG quality |

## Configuration

`config/k1_web.yaml` (rebuild after editing it in place, or pass your own copy with `web_params_file:=`). Every parameter is commented there; the main groups:

- **Motion and safety:** `joint_limits_min/max`, `max_joint_velocity`, `control_rate_hz`, `deadman_timeout_sec`, `state_timeout_sec`.
- **Agent policy:** `agent_allow_motion`, `agent_allow_enable`, `agent_free_motion`, `agent_joint_limits_min/max`, `agent_max_joint_velocity` (see [safety.md](../../docs/safety.md#2-k1_webs-agent-policy)).
- **Actions:** `goal_tolerance_rad`, `settle_timeout_sec`, `ready_timeout_sec`, `gripper_settle_sec`, `gripper_tolerance_m`.
- **Camera and page:** `image_topic`, `camera_rotate_180`, `video_max_fps`, `state_rate_hz`, `secure_cookie`, `host`, `port`.
- **Poses:** `poses_file` (defaults, `config/poses.yaml`) and `user_poses_file` (where **Save current pose…** writes, `~/.k1_web/poses.yaml`).

Environment: `K1_WEB_TOKEN` (required), `K1_WEB_AGENT_TOKEN` (optional, agents only), `K1_WEB_ADMIN_PASSWORD` (optional, testing).

## Camera

`v4l2_camera` publishes `/image_raw` and `/image_raw/compressed` (JPEG); the page, `/snapshot.jpg` and the agent use the compressed topic. The wrist camera is mounted upside down, so k1_web turns its frames 180° (`camera_rotate_180`); the ROS topics, and recordings of them, stay as the camera sends them. To record: `ros2 bag record /image_raw/compressed /joint_states /tf /joint_position_command /gripper_command`.

## ROS 2 interface

k1_web serves the standard motion interfaces under the names ros2_control would use, so MoveIt and other ROS tools can drive the arm. Every goal gets the same limits, speed cap, stale-state check and STOP as the page.

| Name | Type | Notes |
|---|---|---|
| `arm_controller/follow_joint_trajectory` | `control_msgs/action/FollowJointTrajectory` | joint names `joint1` … `joint6` or the SDK's names; joints left out hold still; waypoints are interpolated linearly |
| `gripper_controller/gripper_cmd` | `control_msgs/action/GripperCommand` | `position` in metres as in the URDF: 0 open, about −0.036 closed; fingers that stop on an object finish with `stalled: true` |
| `/tf`, `/robot_description` | | from `robot_state_publisher`: `world`, `base_link`, `link_grasping_frame` (between the fingers), `usb_cam_link` |
| `enabled` | `std_msgs/Bool` (latched) | the driver's motor state; the page follows it |

A trajectory is refused (`INVALID_GOAL`, reason in `error_string`) if the motors are off, a waypoint is outside the limits, or a joint is unknown. It aborts on STOP, a goal from the page, disabling the motors, a newer trajectory, or not reaching the goal within `goal_tolerance_rad` in time (the trajectory's time, plus what the speed cap needs, plus `settle_timeout_sec`). A running goal counts as a connected client for the deadman.

```bash
ros2 run k1_examples move joint1=0.3 --time 2
ros2 action send_goal /gripper_controller/gripper_cmd control_msgs/action/GripperCommand "{command: {position: 0.0}}"
ros2 run tf2_ros tf2_echo world link_grasping_frame
```

## HTTP and WebSocket API

Authenticate with the session cookie or `Authorization: Bearer <token>` (the main token, the admin password, or the agent token). `GET /snapshot.jpg` returns the latest frame (503 if none), `GET /video.mjpeg` a stream, and `WS /ws` speaks JSON. The agent wraps all of this in a blocking client (`k1_agent.client.K1Client`).

Any message counts as a heartbeat; send at least one per second while moving. Add `"id": <any>` to get exactly one reply carrying it (`ack`, `error`, `enable_result` or `info`).

| Message | Effect |
|---|---|
| `{"type":"ping"}` | heartbeat only |
| `{"type":"hello","role":"agent"}` | say who you are (`human` is the default; the agent token can only be `agent`) |
| `{"type":"enable","value":true}` | lock (`true`) or release (`false`) the motors; reply `enable_result` |
| `{"type":"goal","positions":[6 floats]}` | arm goal in radians, clamped to the limits |
| `{"type":"gripper","value":0.0..1.0}` | 1 open, 0 closed |
| `{"type":"pose","name":"zero"}` | move to a saved pose |
| `{"type":"stop"}` | hold the current position; broadcast as an `event` |
| `{"type":"save_pose","name":"..."}` | save the measured pose; reply `info` |
| `{"type":"agent_note","text":"..."}` | a line in every page's agent log |
| `{"type":"agent_prompt","text":"...","mode":"tools","model":"..."}` | (human) give a task to the agent service (`k1-agent serve`) |
| `{"type":"agent_status","status":{...}}` | (agent) the service's state (`idle` / `running`, provider, model, models, profile) |
| `{"type":"agent_question","qid":"...","text":"...","kind":"ask"}` | (agent) ask the people on the page; `kind` is `ask`, `confirm` or `reset` |
| `{"type":"agent_answer","qid":"...","text":"..."}` | (human) answer a question |

Messages from agent connections go through the agent policy first: a refused one gets an `error` starting "k1_web refuses …".

The server pushes:
- `state` at `state_rate_hz`: `names` (always the SDK's: `shoulder_pan` … `gripper_roll`, `gripper`), `positions` (the last is the finger position in metres), `stale`, `enabled`, `ready`, `goal`, `commanded`, `gripper`, `limits_min/max` (an agent's own bounds on agent connections), `poses`, `clients` (counts by role), `agent` (the service's status) and `agent_policy`;
- `error` when a command is rejected;
- `event` (`{"name":"stop","by":role}`) to everyone on STOP; an agent aborts its task when a person presses it.

## Tests

```bash
python -m pytest test          # from this folder, no ROS needed
colcon test --packages-select k1_web && colcon test-result --verbose
```
