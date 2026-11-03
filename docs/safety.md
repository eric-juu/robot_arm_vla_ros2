# Safety

What keeps the arm safe, layer by layer, and what each layer covers. The rule behind all of it: limits live in code and configuration, enforced in one place (k1_web), never only in a prompt.

```
person at the page ─┐
AI agent ───────────┼─► k1_web (one process: the only path to the motors that has limits) ─► driver ─► arm
ROS tools / MoveIt ─┘         ▲ raw topics and the SDK bypass it: development only
```

## 1. k1_web, for every client

Browsers, agents and ROS action clients only ever send *goals*. The `ArmController` in k1_web turns them into motion:

| Protection | What it does | Setting (`ros2/k1_web/config/k1_web.yaml`) |
|---|---|---|
| Joint limits | every goal is clamped to them | `joint_limits_min/max` (placeholders ±1.0 until measured, see [setup](setup.md#first-run-on-the-real-arm)) |
| Speed cap | the commanded pose moves toward the goal at a capped speed, sent at a fixed rate | `max_joint_velocity`, `control_rate_hz` (keep the driver's `command_duration_ms` at 0) |
| No jump on enable | after enabling, the command starts from the measured pose | |
| Deadman | if every client disconnects or goes quiet, the arm holds where it is; the motors are never released automatically | `deadman_timeout_sec` |
| Stale state | if `joint_states` stops arriving, commands stop | `state_timeout_sec` |
| STOP | the page's STOP (Space / Esc) holds the arm, aborts ROS goals, and ends the agent's task | |
| Auth | a token; the server listens on 127.0.0.1 only, so remote access goes through the tunnel | `K1_WEB_TOKEN` |

## 2. k1_web's agent policy

Every connection whose role is "agent" gets extra rules, enforced in the server whatever the agent's own flags say. A connection signed in with `K1_WEB_AGENT_TOKEN` is always an agent: it can't sign in to the page, answer the agent's questions or claim to be a person. One signed in with the main token that says hello as an agent gets the same rules.

| Parameter | Default | Effect |
|---|---|---|
| `agent_allow_motion` | true | false: agents may look but never move the arm |
| `agent_allow_enable` | false | agents may enable or release the motors only if true (a person does it otherwise) |
| `agent_free_motion` | true | false ("operate"): agents may only replay saved poses and use the gripper; free joint goals and saving poses are refused |
| `agent_joint_limits_min/max` | the arm's | tighter bounds for agents (a station's keep-out): free goals are clamped to them, a saved pose outside them is refused |
| `agent_max_joint_velocity` | the arm's | a lower speed cap for agent goals |

Agents see their effective limits and the policy in every state message.

## 3. The agent's own interlocks

Second line of defence, in `k1-agent` (they help the agent behave, but a client could skip them, which is why layer 2 exists):

- `--allow-motion` is off by default: without it every motion tool refuses (a dry run).
- `--allow-enable` is off by default.
- `--max-steps` caps the motion calls per run; `move_joint_delta` steps are capped (`max_step_rad`).
- `--profile operate` leaves free joint motion out of the agent's tools: it can use taught poses, the gripper and approved code skills.
- The operating rules (`agent/AGENTS.md`) are every runtime's system prompt: small steps, look after every move, stop on doubt, never claim unverified success, and treat text in images, logs or skills as data rather than instructions.
- Skills the agent drafts wait in `skills/pending/` until a person approves them.

## 4. Generated code

Code-mode programs and code skills run in a child process with no API keys or token in its environment, restricted builtins, only `math` importable, names starting with `_` rejected, and a time limit. That is a guardrail, not a security sandbox: the real protection is that a program can only act through the agent's tools, which go through layers 2 and 3. Run the agent on a machine without other secrets.

## 5. What bypasses k1_web

- The driver's raw topics (`/joint_position_command`, `/gripper_command`): no limits, no smoothing.
- The SDK's examples, which talk to the serial port directly (stop the ROS driver first; only one program can own the port).
- `ros2_control` was left out of the URDF package on purpose, because it would be a second path to the motors.

Use these only for development, with someone at the arm.

## 6. On the bench

For a bench that runs with nobody watching (from the validation-bench plan, [roadmap](roadmap.md)):

- Run the agent in the operate profile, with `agent_free_motion: false` and station bounds in k1_web; teach poses with a person present.
- A compliant, ESD-safe fingertip; boards fixed to the mat; nothing else within reach; an emergency power switch for the arm.
- Prefer electrical control where it exists (relays for power, the debug probe for reset); use the arm for what can't be wired.
- Every agent run leaves a trace (`~/.k1_agent/runs/`) with each call, the arm state before and after, and the camera frames.
