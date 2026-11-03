# k1-agent

An AI agent for the K1. It drives the arm through the same k1_web API as the web page, so every move gets k1_web's limits, speed cap, deadman and agent policy ([docs/safety.md](../docs/safety.md)). The design follows NVIDIA's [ASPIRE](https://research.nvidia.com/labs/gear/aspire/):

- **One set of tools** (`k1_agent/tools.py`): `observe`, `move_joints`, `move_joint_delta`, `go_pose`, `set_gripper`, `stop`, skills, `ask_operator`, `done`… Failures come back typed (`not_reached`, `clamped`, `safety_interlock`, `unavailable`…), so the model can reason about them. The agent's own loop, the MCP server and code mode are all generated from it, with the same operating rules ([AGENTS.md](AGENTS.md)).
- **Two modes:** `tools` (one call at a time, a fresh camera frame after every move) and `code` (the model writes a program; on failure it diagnoses it from the trace and repairs it).
- **Two profiles:** `teach` (free joint motion) and `operate` (taught poses, the gripper and approved code skills only).
- **Traces:** every call, with the arm state before and after and a camera frame, in `~/.k1_agent/runs/<time>-<task>/`.
- **A skill library** (`skills/`): notes on what works on this arm, and *code skills* (a `.py` program next to the note) that replay a tested routine without improvising. Drafts wait in `skills/pending/` for a person's approval.
- **Pluggable models:** Claude, OpenAI or Gemini through their APIs, or Claude Code / Codex signed in with a subscription.
- **Evaluation** (`k1-agent eval`): see [evals/README.md](evals/README.md).

No GPU or ROS is needed where the agent runs: the model runs in the provider's cloud.

## Install

On the Linux PC, or any machine that can reach the web page (Windows works too):

```bash
cd ~/ros2_ws/src/robot_arm_vla_ros2/agent
python3 -m venv ~/.k1_agent/venv && source ~/.k1_agent/venv/bin/activate
pip install -e ".[all]"          # or only some of: .[anthropic] .[openai] .[gemini] .[mcp]; .[sim] for evaluations
```

Keep the `-e`: the agent reads `config.yaml`, `AGENTS.md` and `skills/` from this folder. Put the API keys you need in `~/.k1_agent/keys.sh` (`chmod 600`) and `source` it: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`.

## Running a task

1. Start the bringup, open the web page and **enable the motors**. Keep the page open: STOP there aborts the agent.
2. In another terminal:
   ```bash
   source ~/.k1_agent/venv/bin/activate && source ~/.k1_web/token.sh && source ~/.k1_agent/keys.sh
   k1-agent observe                                             # connection check: prints the state, saves frame.jpg
   k1-agent run "turn the base left by about 0.3 rad, then come back" --allow-motion
   k1-agent run "pick up the red block" --allow-motion --mode code
   k1-agent run "press the reset button" --allow-motion --profile operate --provider openai --model <id>
   ```
   Without `--allow-motion` every motion is refused (a safe dry run). The agent narrates in the page's **Agent log** and asks its questions in the terminal (in code mode, also to reset the scene between attempts).
3. After a success it offers to draft a skill (with the program, in code mode): `k1-agent skills pending`, `skills show <name>`, `skills approve <name>` or `skills reject <name>`.

To run the agent on another machine, add `--url https://<link>.trycloudflare.com` (or set `url:` in `config.yaml`). With `K1_WEB_AGENT_TOKEN` set, the agent signs in with it; otherwise with `K1_WEB_TOKEN`.

### From the web page

`k1-agent serve --allow-motion` stays connected and runs the tasks people type into the page's **Agent** card, one at a time. With `--models a,b,c` (or `serve_models:` in `config.yaml`) the card gets a model menu; the first is the default. Questions appear in the card (with Yes/No buttons for confirmations), STOP ends the current task, and the safety flags and the model list come only from the command line, never from the page.

## Models

| Provider | `--provider` / in `--models` | Billed to | Setup |
|---|---|---|---|
| Anthropic, OpenAI, Gemini APIs | `anthropic`, `openai`, `gemini`; a bare model id picks the provider from its name | the API key | the key in the environment |
| Claude Code | `claude-code` / `claude-code:<model>` | the signed-in Claude account | install Claude Code, `k1-agent login` (headless: `claude setup-token`, then `CLAUDE_CODE_OAUTH_TOKEN`) |
| Codex | `codex` / `codex:<model>` | the signed-in ChatGPT account | install Codex and `codex-code-mode-host` next to it, `codex login` (`--device-auth` without a browser) |

`<model>@<effort>` sets a reasoning effort (API models). Model ids and the default for each provider are in `config.yaml`; prices in `pricing.yaml`.

Claude Code and Codex run headless, one session per task, and are locked down the same way:
- their only tools are the k1 MCP tools (Claude Code: `--tools ""`; Codex has no such switch, so its own tools are turned off one by one and the rest is fenced into a read-only sandbox; recheck `k1_agent/codex.py` after upgrading Codex);
- the MCP server they start runs with this run's `--allow-motion`, `--allow-enable`, `--max-steps` and `--profile`;
- their system prompt is AGENTS.md, and they finish with the `done` tool (a session that ends without it counts as a failure);
- questions go to the web page, and STOP on the page ends the session;
- the provider's API key is removed from their environment so the subscription pays (`use_api_key: true` in `config.yaml` bills the key instead). Codex reports tokens, not cost, so its cost is estimated from `pricing.yaml`.

## Profiles and code skills

`--profile operate` (or `profile:` in `config.yaml`) leaves free joint motion out of the agent's tools: it can look, use saved poses and the gripper, and run approved code skills. For an unattended bench, pair it with `agent_free_motion: false` in k1_web, which enforces the same thing on the server.

A code skill is `skills/<name>.py` defining `run(k1)` against the code-mode API, next to `skills/<name>.md`. Successful code-mode runs draft one; so can an agent with `save_skill_draft(name, markdown, code)`. Once approved:

```bash
k1-agent skills run <name> --repeat 50 --reset-pose home --allow-motion    # replay without a model: "49/50 runs passed"
```

The agent runs it with the `run_skill` tool. That turns one paid success into a free, repeatable action, and `--repeat` measures how reliable it is.

## Claude Code or Codex as an interactive agent

```bash
claude mcp add k1 -- ~/.k1_agent/venv/bin/k1-agent mcp --allow-motion        # add --profile operate, --web-operator...
```

Start `claude` after sourcing `token.sh`, so the MCP server inherits the token. The operating rules come with the server's instructions. Motion is limited the same way: no `--allow-motion` means no moves.

## Settings

`config.yaml` is commented: the provider and model ids, the Claude Code / Codex options, the mode and profile, run limits (`max_turns`, `attempts`, `program_timeout`, `max_steps`, `max_step_rad`), `image_history` (frames kept in the conversation), and where traces, eval results and skills live (`runs_dir`, `results_dir`, `skills_dir`). Command-line flags override it.

## Troubleshooting

| Problem | Fix |
|---|---|
| `no reply to 'hello'` / `k1_web did not answer 'hello'` | The web server runs older code: on the arm PC, `git pull`, `colcon build`, source, restart the launch. |
| `could not connect … 401` / `HTTP 403` | The token isn't set in this terminal, or differs from the server's: `source ~/.k1_web/token.sh`. |
| Every move returns `not_ready` | The motors are off: enable them on the page. |
| Every move returns `safety_interlock` | Add `--allow-motion`. |
| A move returns `rejected` with "k1_web refuses …" | k1_web's agent policy forbids it ([docs/safety.md](../docs/safety.md#2-k1_webs-agent-policy)). |
| A tool returns `unavailable` | Not in this session: the profile, `--allow-enable`, or no operator. |
| `Claude Code is not signed in` | `k1-agent login` (or set `CLAUDE_CODE_OAUTH_TOKEN`). |
| Codex: "required MCP servers failed to initialize" | Install `codex-code-mode-host` next to `codex`; check that `k1-agent mcp` starts with that `--url`. |
| The page says "no agent service is running" | Start `k1-agent serve` on the PC and keep that terminal open. |

## Tests

```bash
pip install -e ".[test,mcp]"
python -m pytest tests          # the real k1_web app with a simulated arm: no ROS, no API keys
```
