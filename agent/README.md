# k1-agent

An AI agent for the K1 arm. It drives the arm through the same `k1_web` API as the web page, so every move gets the server's joint limits, speed limit and stop-on-disconnect. The design follows NVIDIA's [ASPIRE](https://research.nvidia.com/labs/gear/aspire/):

- **A fixed primitive API** (`k1_agent/primitives.py`): `observe`, `move_joints`, `move_joint_delta`, `go_pose`, `go_home`, `set_gripper`, `stop`, `wait`. Failures come back as typed results (`not_reached`, `clamped`, `safety_interlock`, …) the model can reason about.
- **Traces:** every call is written to `runs/<time>-<task>/trace.jsonl` with the arm state before and after, plus a camera frame.
- **Two modes:**
  - `tools`: the model calls one primitive at a time and sees every frame.
  - `code`: the model writes a program (code-as-policy). When the program fails, the model diagnoses it from the trace and repairs it.
- **A skill library** (`skills/*.md`): notes on what works on this arm, read by the agent. New skills the agent drafts wait in `skills/pending/` until you approve them.
- **An MCP server**, so Claude Code or Codex can be the agent (ASPIRE's own setup).
- **Pluggable LLMs:** Claude (Anthropic API), OpenAI (GPT-5.x now, GPT-6 Astra later by changing the model name) or Google Gemini (for example Gemini 4 Argon).
- **Model comparison** (`k1-agent eval`): success rate against cost for several models on the same tasks, in a simulated arm or on the real one. See [Comparing models](#comparing-models).

No GPU or ROS is needed on the machine running the agent: the model runs in the provider's cloud.

## Install (on the Linux PC, or any machine that can reach the web page)

```bash
cd ~/Downloads/robot_arm_vla_ros2/agent
python3 -m venv ~/.k1_agent/venv
source ~/.k1_agent/venv/bin/activate
pip install -e ".[all]"          # or .[anthropic], .[openai], .[gemini], .[mcp]
```

Keep the `-e`: the agent reads `config.yaml` and `skills/` from this folder.

API keys (put them in `~/.k1_agent/keys.sh`, `chmod 600`, and `source` it):

```bash
export ANTHROPIC_API_KEY=...     # for provider: anthropic
export OPENAI_API_KEY=...        # for provider: openai
export GEMINI_API_KEY=...        # for provider: gemini
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

## Claude Code as the model (sign in, no API key)

With `--provider claude-code` the agent is a headless Claude Code session, billed to the Claude account Claude Code is signed in with instead of an API key.

1. Install Claude Code on the PC that runs the agent (https://claude.com/claude-code), then sign in once:
   ```bash
   k1-agent login            # runs `claude auth login`; opens a browser or prints a link
   k1-agent login --status   # check
   ```
   On a PC without a browser, run `claude setup-token` on any machine and `export CLAUDE_CODE_OAUTH_TOKEN=...` (in `~/.k1_agent/keys.sh`).
2. Run tasks as usual:
   ```bash
   k1-agent run "point the camera at the cup" --provider claude-code --allow-motion
   ```
   Or set `provider: claude-code` in `config.yaml`. `claude_code.model` (or `--model sonnet|opus|haiku`) picks the model.

How it is locked down:
- **Tools:** Claude Code gets only the k1 MCP tools. `--tools ""` turns off its shell, file and web tools.
- **Same limits:** the MCP server runs with this run's `--allow-motion`, `--allow-enable` and `--max-steps`, so the interlocks match `k1-agent run`.
- **Questions:** they appear on the web page, not in the terminal.
- **STOP:** STOP on the page ends the session.
- **API key:** `ANTHROPIC_API_KEY` is removed from Claude Code's environment, so the signed-in account pays. To bill the key instead, set `claude_code.use_api_key: true`.
- **Traces:** the run's narration and tool calls go to `runs/<time>-<task>/`. The MCP server's own trace, with frames, goes to `runs/<time>-mcp/`.

## Prompting from the web page

`k1-agent serve` keeps the agent connected and runs the tasks people type into the page's **Agent** card, one at a time:

```bash
k1-agent serve --provider claude-code --allow-motion      # or --provider anthropic / openai
```

- **The card shows:**
  - the provider and model;
  - whether the agent is ready or running;
  - "dry run" when the service has no `--allow-motion`.
- **Sending a task:** type it and press Enter (Shift+Enter adds a new line). Pick `tools` or `code` mode first.
- **Agent questions:**
  - questions show up in the card with an answer box;
  - yes/no checks, such as "save a skill draft?", get Yes and No buttons;
  - between code-mode attempts, a "scene is reset" button.
- **STOP** (Esc, or Space when not typing) ends the current task. The service then waits for the next prompt.
- **What the page can't change:** the safety flags. They come only from the `serve` command line.

## Using Claude Code (or Codex) as the agent

```bash
claude mcp add k1 -- ~/.k1_agent/venv/bin/k1-agent mcp --allow-motion
```

Start `claude` in this folder, after sourcing `token.sh`, so the MCP server inherits `K1_WEB_TOKEN`. Tell it to follow `AGENTS.md`, then give it a task. Its motion is limited the same way: no `--allow-motion` means no moves, plus the `max_steps` budget and k1_web's limits.

## Settings (`config.yaml`)

| Setting | Meaning |
|---|---|
| `provider`, `models` | which LLM: `anthropic`, `openai`, `gemini` or `claude-code`, and the model id for each |
| `claude_code` | Claude Code's command, model, whether to use an API key, a per-task timeout |
| `mode` | `tools` or `code` |
| `max_turns`, `attempts`, `program_timeout` | how long a run may go on |
| `max_steps` | motion calls per run (`--max-steps`) |
| `max_step_rad` | cap for `move_joint_delta` |
| `image_history` | camera frames kept in the conversation (more frames cost more tokens) |
| `runs_dir`, `skills_dir` | where traces and skills live |

**Switching to GPT-6 Astra:** set `provider: openai` and put the Astra model id under `models.openai` (check the exact id in OpenAI's docs once you have access). Nothing else changes.

## Comparing models

Step-by-step guide: [evals/README.md](evals/README.md).

`k1-agent eval` runs the same tasks with several models and reports **success rate against cost**, the way ASPIRE compares agents. A task's checker decides success, never the model's own claim. A model that says "done" without doing the task fails the trial, and it's counted as a *false success*.

There are three tiers. Tiers 0 and 1 run on any computer, including Windows, with no ROS and no arm. Only tier 2 needs the arm PC.

| Tier | What it tests | Where | Command |
|---|---|---|---|
| 0 | **Perception:** reading wrist-camera frames ("is the gripper open?", "is the block between the fingers?") | anywhere, from saved frames | `eval label`, `eval perception` |
| 1 | **Decision-making:** tool use, following the rules, honesty (limits, a blocked joint, STOP, asking when unclear) | a simulated arm (the real k1_web server, in-process) | `eval run --tier 1` |
| 2 | **End to end:** the real arm, with an operator who resets the scene and judges each trial on the web page without being told the model | the real arm (`--url`) | `eval run --tier 2` |

The tasks are in `evals/tasks.yaml`, with the checks explained at the top of the file.

### Running a comparison

```bash
pip install -e ".[all,test]"      # the test extras include the simulator's web server
# API keys as above; Claude Code models need `k1-agent login`

# a pilot: 1 trial per task, to see what a full run costs
k1-agent eval run --models claude-haiku-5-5,claude-opus-5-5,gpt-5.5,gemini-4-argon,claude-code:sonnet --trials 1

# the full run: 10 trials, 4 simulated arms side by side, stop starting new trials after $50
k1-agent eval run --models ... --trials 10 --parallel 4 --max-cost-usd 50

k1-agent eval report evals/results/<run> --html report.html
```

- **Models:**
  - A bare id picks the provider from its name (`claude-…`, `gpt-…`, `gemini-…`).
  - Otherwise write `provider:model`, for example `claude-code:sonnet`.
  - Add `@effort` for an effort or reasoning level, for example `gpt-5.5@high` or `claude-opus-5-5@low` (OpenAI and Claude API only).
- **Other options:**
  - `--modes tools,code`, and `--no-skills` for the ablation, run the ASPIRE variations.
  - `--tasks S1-single-joint,…` picks tasks.
  - `--resume <folder>` continues an interrupted run, skipping finished trials.
- **Order:** trials run in shuffled model order, so slow drift (rate limits, on the real arm lighting or battery) affects every model alike.
- **Output:** each trial writes one row to `<run>/results.jsonl`. A row holds:
  - success, claimed success, cost, tokens and time;
  - the real model id;
  - the git commit, config, prompts and skills version it ran with.

  Its full trace goes to `<run>/traces/`. Each run uses its own copy of the skill library, so skill drafts written during trials stay in the run folder.
- **Report:**
  - a success-vs-cost table with 95% intervals, $ per success and false successes;
  - a per-task table;
  - an HTML page with the scatter plot. Models marked `*` are on the frontier: nothing else is both cheaper and at least as successful.

### Perception (tier 0)

```bash
# copy run traces from the arm PC first, e.g. scp -r armpc:~/Downloads/robot_arm_vla_ros2/agent/runs ./arm-runs
k1-agent eval label ./arm-runs                                    # automatic: gripper open/closed
k1-agent eval label ./arm-runs --question "Is the block between the fingers?" --category grasp
k1-agent eval perception --models claude-opus-5-5,gemini-4-argon,gpt-5.5 --repeats 3
```

- **Labelling:** `label` copies frames into `evals/frames/` and adds items to `evals/perception.jsonl`. With `--question` it opens each frame and asks you for the answer.
- **Old frames:** frames recorded before the camera-flip fix are upside down. Add `--rotate-180` so the models see them the way the agent sees frames now.
- **Scoring:** every question also offers "can't tell", which counts as an abstention, not a wrong answer. The report gives accuracy, abstentions, *confidently wrong* answers, and $ per 100 questions.

### Cost

- **API models:** tokens come from every API call and are priced from `pricing.yaml`. Check its rates against the providers' pricing pages before a big run. A model missing from the file shows `$?`.
- **Claude Code:** the cost is Claude Code's own API-equivalent estimate. It's billed to your subscription, not to an API key.

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
| `Claude Code is not signed in` | Run `k1-agent login` (or set `CLAUDE_CODE_OAUTH_TOKEN`). |
| The page says "no agent service is running" | Start `k1-agent serve` on the PC, and keep that terminal open. |

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
