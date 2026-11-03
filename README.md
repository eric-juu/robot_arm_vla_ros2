# robot_arm_vla_ros2

Software for the Unitree K1 arm (an NXROBO Sagittarius: 6 joints, a gripper and a wrist camera), as a ROS 2 workspace plus an AI agent. The goal is a robotic hand for post-silicon validation benches: pressing buttons on dev boards, reading LEDs and displays, and triaging failed tests.

```
LLM (API models, or Claude Code / Codex through MCP)
 └─ agent/            k1-agent: tools, traces, skills, `k1-agent eval`
     └─ WebSocket ──  ros2/k1_web          web page + the safety layer every motion goes through
                      │                    (limits, speed cap, deadman, agent policy) + ROS actions
                      └─ ROS topics ──     ros2/k1_driver_py ── ros2/unitree_k1_sdk ── serial ── arm
ros2/k1_description: URDF → /tf      ros2/k1_examples: ROS clients      cloudflared: remote link
```

| Part | What it is |
|---|---|
| [`ros2/unitree_k1_sdk`](ros2/unitree_k1_sdk/README.md) | The serial protocol, as a plain Python library |
| [`ros2/k1_driver_py`](ros2/k1_driver_py/README.md) | ROS 2 driver: `joint_states`, `enabled`, `enable`, raw command topics |
| [`ros2/k1_description`](ros2/k1_description/README.md) | URDF and meshes, for `/tf` and RViz |
| [`ros2/k1_web`](ros2/k1_web/README.md) | Browser control with the live camera, the safety layer, `FollowJointTrajectory` / `GripperCommand`, the bringup launch file |
| [`ros2/k1_examples`](ros2/k1_examples/README.md) | `ros2 run k1_examples move/gripper/zero/demo`, and a client class |
| [`agent`](agent/README.md) | The AI agent (`k1-agent`), its MCP server, and the model evaluation ([`agent/evals`](agent/evals/README.md)) |

Everything runs on the Linux PC the arm is plugged into (Ubuntu 24.04 + ROS 2 Jazzy, or 22.04 + Humble). Any other device only needs a browser. The agent and its evaluation also run on any computer, Windows included.

## Every session

On the Linux PC, after the [one-time setup](docs/setup.md):

```bash
source ~/.k1_web/token.sh
ros2 launch k1_web k1_bringup.launch.py port:=/dev/ttyACM0 tunnel:=true     # no arm: mock:=true use_camera:=false
```

1. Open the boxed `https://….trycloudflare.com` link from the output (a new one every session; give it ~30 s). Sign in with the token (`echo $K1_WEB_TOKEN`).
2. Click **Enable motors**, then drive with the sliders and pose buttons. **STOP** (or Space / Esc) holds the arm.
3. When done: move to a safe pose, support the arm, **Disable motors** (it goes limp), then Ctrl+C in the terminal.

To let the AI agent do a task, see [agent/README.md](agent/README.md):

```bash
k1-agent run "turn the base left a little" --allow-motion
```

## Documentation

| Doc | For |
|---|---|
| [docs/setup.md](docs/setup.md) | Installing, updating, the first run on the real arm, remote access, troubleshooting |
| [docs/safety.md](docs/safety.md) | Every safety layer in one place: what stops the arm, and what each one covers |
| [agent/README.md](agent/README.md) | Running the agent, from the terminal, the web page, Claude Code or Codex |
| [agent/evals/README.md](agent/evals/README.md) | Comparing models and checking changes: probes, simulated tasks, bench incidents, the real arm |
| [docs/roadmap.md](docs/roadmap.md) | What comes next (the validation bench layer) |
| Package READMEs | Reference: interfaces, parameters, launch arguments |

## Tests

```bash
cd ros2/k1_web && python -m pytest test          # no ROS needed; so do the SDK and the agent:
cd agent && python -m pytest tests               # after pip install -e ".[test,mcp]"
cd ~/ros2_ws && colcon test && colcon test-result --verbose     # everything, on the ROS PC
```

The ROS-free suites run on every push ([.github/workflows/tests.yml](.github/workflows/tests.yml)).
