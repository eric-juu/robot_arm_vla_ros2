# k1-agent

An AI agent for the K1 arm. It drives the arm through the same `k1_web` API as the web page, so every move gets the server's joint limits, speed limit and stop-on-disconnect. The design follows NVIDIA's [ASPIRE](https://research.nvidia.com/labs/gear/aspire/):

- **A fixed primitive API** (`k1_agent/primitives.py`): `observe`, `move_joints`, `move_joint_delta`, `go_pose`, `go_home`, `set_gripper`, `stop`, `wait`. Failures come back as typed results (`not_reached`, `clamped`, `safety_interlock`, …) the model can reason about.
- **Traces:** every call is written to `runs/<time>-<task>/trace.jsonl` with the arm state before and after, plus a camera frame.
- **Two modes:**
  - `tools`: the model calls one primitive at a time and sees every frame.
  - `code`: the model writes a program (code-as-policy). When the program fails, the model diagnoses it from the trace and repairs it.
- **A skill library** (`skills/*.md`): notes on what works on this arm, read by the agent. New skills the agent drafts wait in `skills/pending/` until you approve them.
- **An MCP server**, so Claude Code or Codex can be the agent (ASPIRE's own setup).
- **Pluggable LLMs:** Claude (Anthropic API) or OpenAI (GPT-5.x now, GPT-6 Astra later by changing the model name).

No GPU or ROS is needed on the machine running the agent: the model runs in the provider's cloud.

## Install (on the Linux PC, or any machine that can reach the web page)

```bash
cd ~/Downloads/robot_arm_vla_ros2/agent
python3 -m venv ~/.k1_agent/venv
source ~/.k1_agent/venv/bin/activate
pip install -e ".[all]"          # or .[anthropic], .[openai], .[mcp]
```

Keep the `-e`: the agent reads `config.yaml` and `skills/` from this folder.

API keys (put them in `~/.k1_agent/keys.sh`, `chmod 600`, and `source` it):

```bash
export ANTHROPIC_API_KEY=...     # for provider: anthropic
export OPENAI_API_KEY=...        # for provider: openai
```

## Every session

1. Start the arm and web page as usual (`ros2 launch k1_web k1_bringup.launch.py ...`).
2. Open the web page, **enable the motors**, and keep the page open: STOP (Space / Esc) there aborts the agent's task.
3. In another terminal:
   ```bash
   source ~/.k1_agent/venv/bin/activate
   source ~/.k1_web/token.sh        # the agent signs in with K1_WEB_TOKEN
   source ~/.k1_agent/keys.sh
   k1-agent observe                 # connection check: prints the state, saves frame.jpg
   ```
4. Run a task:
   ```bash
   k1-agent run "turn the base left by about 0.3 rad, then come back" --allow-motion
   k1-agent run "pick up the red block" --allow-motion --mode code
   k1-agent run "..." --provider openai --model gpt-5.5 --allow-motion
   ```
   Without `--allow-motion` every motion is refused, which is a safe dry run. The agent posts its reasoning to the page's **Agent log**, and asks you questions in the terminal. In code mode it also asks you to reset the scene between attempts.
5. After a success it offers to draft a skill. Review it, then:
   ```bash
   k1-agent skills pending
   k1-agent skills show <name>
   k1-agent skills approve <name>     # or: reject <name>
   ```

To run the agent on another machine, use `--url https://<link>.trycloudflare.com` (or set `url:` in `config.yaml`).

## Using Claude Code (or Codex) as the agent

```bash
claude mcp add k1 -- ~/.k1_agent/venv/bin/k1-agent mcp --allow-motion
```

Start `claude` in this folder, after sourcing `token.sh`, so the MCP server inherits `K1_WEB_TOKEN`. Tell it to follow `AGENTS.md`, then give it a task. Its motion is limited the same way: no `--allow-motion` means no moves, plus the `max_steps` budget and k1_web's limits.

## Settings (`config.yaml`)

| Setting | Meaning |
|---|---|
| `provider`, `models` | which LLM: `anthropic` or `openai`, and the model id for each |
| `mode` | `tools` or `code` |
| `max_turns`, `attempts`, `program_timeout` | how long a run may go on |
| `max_steps` | motion calls per run (`--max-steps`) |
| `max_step_rad` | cap for `move_joint_delta` |
| `image_history` | camera frames kept in the conversation (more frames cost more tokens) |
| `runs_dir`, `skills_dir` | where traces and skills live |

**Switching to GPT-6 Astra:** set `provider: openai` and put the Astra model id under `models.openai` (check the exact id in OpenAI's docs once you have access). Nothing else changes.

## Safety

- **k1_web still enforces everything it did for the web page:** joint limits, the speed limit, and holding position on disconnect or stale state.
- **Interlocks:**
  - `--allow-motion` is off by default.
  - `--allow-enable` is off by default, so a human enables the motors.
  - `max_steps` caps how many moves a run makes.
  - `move_joint_delta` steps are capped.
- **STOP:** a STOP from any browser aborts the agent's task immediately. A new run is needed to continue.
- **Generated programs (code mode):**
  - They run in a child process with no API keys or token in its environment.
  - Builtins are restricted, and only `math` can be imported.
  - Names starting with `_` are rejected, and a time limit applies.
  - This is a guardrail, not a security sandbox. Run the agent on a machine without other secrets, as ASPIRE also advises.

## Troubleshooting

| Problem | Fix |
|---|---|
| `no reply to 'hello'` / `k1_web did not answer 'hello'` | The web server is older than the agent. On the arm PC: `git pull`, `colcon build --base-paths ~/Downloads/robot_arm_vla_ros2/ros2`, `source ~/ros2_ws/install/setup.bash`, and restart `ros2 launch`. The page should then show an **Agent log** panel. |
| `could not connect … 401` / `HTTP 403` | `K1_WEB_TOKEN` isn't set in this terminal, or it differs from the one the server started with. Run `source ~/.k1_web/token.sh`. |
| Every move returns `not_ready` | The motors are off. Enable them on the web page. |
| Every move returns `safety_interlock` | Add `--allow-motion`. |

## Tests

```bash
pip install -e ".[test,mcp]"
python -m pytest tests          # starts the real k1_web app with a simulated arm; no ROS, no API keys
```

## Later (see the plan)

The wrist camera alone gives no depth, and object finding relies on the model's own vision. Next steps for precision:
- a K1 URDF with forward/inverse kinematics, for `move_to_pose(xyz)`;
- an open-vocabulary detector, which runs on CPU (slow) or a GPU service;
- a fixed RGB-D camera.

With a GPU host and RGB-D, the MCP tools here can back a port of ASPIRE's own harness.
