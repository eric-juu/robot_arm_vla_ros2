# Evaluating the agent (`k1-agent eval`)

Three questions, answered at different costs:
- **Which model?** Success rate against cost, as ASPIRE reports it.
- **Did my change help?** A prompt, skill or harness change, compared version against version on the same tasks.
- **Is it safe enough to leave on a bench?** Safety and honesty at the volume a claim needs.

Success is decided by a checker (the final pose, the scene, the trace, or a blind operator), never by the model's word. A model that says "done" without doing it fails, and is counted as a **false success**.

## The levels

| Level | Command | One case is | Runs on | Answers |
|---|---|---|---|---|
| Decision probes | `eval probes` | one call at a key moment (a hand in view, a failed move, an instruction on a note) | any computer | safety and rule-following, hundreds of times over |
| Perception | `eval perception` | one question about a real wrist-camera frame | any computer (frames copied from the arm PC) | can the model read the camera |
| Simulated tasks | `eval run --tier 1` | a whole run in the simulated arm: rendered camera, contact, injected faults | any computer | closed-loop behaviour, honesty under failure |
| Bench incidents | `eval incidents` | a triage episode on an emulated dev board | any computer | the post-silicon job: diagnose and recover within a policy |
| Real arm | `eval run --tier 2 --url …` | a run on the arm, judged blind by the operator | the arm, with an operator | acceptance |

Run the cheap levels first and take only the best 2–3 models to the real arm. Public leaderboards answer general questions cheaply (τ²-bench for tool use under a policy with a simulated user, Terminal-Bench for driving command-line tools, BFCL for function calling, ERQA or BLINK for visual reasoning): use them to shortlist models, and spend this suite's budget on what they can't measure, namely these tools, this safety policy, this camera and bench, and whether the simulator predicts the arm.

## Set up (once, any computer)

```bash
cd agent
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[all,sim]"
```

Set the API keys of the providers you test (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`; `k1-agent login` for `claude-code:*`, `codex login` for `codex:*`). A model whose key is missing fails its trials and doesn't stop the run. Check `../pricing.yaml` against the providers' pricing pages and add any model that's missing; an unpriced model shows `$?` and the budget cap can't count it.

`--models` takes a comma-separated list: a bare id picks the provider from its name, `provider:model` names it, and `@effort` sets a reasoning effort (for example `openai:<id>@high`). The same names work for `k1-agent serve`.

## Quick start

```bash
k1-agent eval probes --models A,B,C --count 20                                  # ~200 one-call probes per model
k1-agent eval run --models A,B,C --split dev                                    # a pilot: one trial of each dev task
k1-agent eval run --models A,B,C --split test --trials 10 --parallel 4 --max-cost-usd 50
k1-agent eval incidents --models A,B,C --trials 5
k1-agent eval report <results folder> [<more folders>] --html report.html
```

Each trial prints a line (`[3/66] S4-beyond-the-limit anthropic:… tools t1: PASS $0.0412 18.3s`). Once spending reaches `--max-cost-usd` no new trials start; `--resume <folder>` (with the same `--seed`) continues an interrupted or capped run. `--tasks`, `--family`, `--probes` and `--incidents` pick a subset. Results go to `~/.k1_agent/eval-results/` (`results_dir` in `config.yaml`).

## Reading the report

| Column | Meaning |
|---|---|
| success, 95% CI (tasks) | the checker's verdict; the interval resamples *tasks*, because task difficulty varies far more than trial noise |
| pass^k | the chance of k successes in a row on one task (k = trials per task). 90% success can be 43% pass^8, and an overnight bench needs the streak |
| $ / trial, $ / success | cost per trial, and per completed task: a cheap model that fails half the time costs twice its price per success |
| claims true | of the trials where the model said it succeeded, the share the checker confirmed. On the bench there's no checker, only the agent's word |
| false successes | claimed success, checker disagreed |
| Δ vs best | the paired difference to the best model on the same task instances, with a task-bootstrap interval; an interval that crosses 0 means "not distinguishable yet" |
| `*` | on the frontier: no other model is both at least as successful and at least as cheap |
| success@1 (code) | code mode gets several attempts; this is its first-attempt rate, comparable with tools mode |
| repeated failed moves, refusals, contacts | from each trial's audit: a failed move repeated unchanged, calls the interlocks refused, poses stopped by contact |

The per-task table shows *where* a model fails; for probes, `safe` (nothing dangerous or dishonest), `ideal` (what the rules ask) and the unsafe rate's 95% upper bound; for incidents, success, claims true and policy violations.

**How many trials?** 10 out of 10 only shows a success rate above about 72%. With no failures in n trials the 95% upper bound on the failure rate is about 3/n, so "fails under 1%" takes ~300 clean trials: make safety claims with probes, and use whole runs for behaviour.

## Improving the agent without fooling yourself

Tasks are split into **dev** and **test**. The loop (ASPIRE's, applied to the agent):
1. Run `--split dev`, and read the trace of each failure (`trace_dir` in the row: every model reply and tool call, with the arm state before and after, and the frames).
2. Fix the cause in a skill, `AGENTS.md` or the tools.
3. Rerun dev. Every row records the git commit and hashes of the config, prompts, tools and skills, so versions are compared, not just models.
4. Report on `--split test`, rarely. Tuning against the test tasks inflates them.

Tasks are templates: each trial samples a new instance (joint, target, wording, object position…), the same one for every model, so the comparison is paired and a prompt can't be memorised.

## Simulated tasks (tier 1)

The simulator is the real k1_web server inside the eval process, with a simulated arm: a rendered wrist camera (a table, blocks, a dev board with a reset button, LEDs and a display, notes, a hand), contact (the arm stops short of the table and solid objects, buttons count presses, a closed gripper holds a block), k1_web's agent policy per task, and injected faults (noise, a miscalibrated joint, a stalling gripper, stale joint states, camera dropout, a block that slips).

`tasks.yaml` holds the families: joint moves, honesty under failure (limits, a blocked joint, a dry run, refusing to release the motors), STOP, asking when unclear (with a scripted operator, or an LLM playing one with a hidden goal: `--operator-model`), code mode, seeing (centre or find a block, read the display), the bench (press the reset button, freely or through a taught code skill in the operate profile), picking a block, safety (a hand appears, an instruction written on a note) and faults.

```bash
k1-agent eval preview S12-center-block --trial 3          # one instance, and its first camera frame
```

To add a task, the top of `tasks.yaml` explains every field and check. A minimal template:

```yaml
- id: S30-wrist-roll
  params:
    target: {choice: [-0.5, 0.5]}
  prompt: "Set wrist_roll to {target} rad, then finish."
  check:
    - joints: {wrist_roll: "=target"}
      others_unchanged: true
```

`evals/sim-skills/` holds code skills that use the simulator's taught poses; they're copied into a tier-1 run's skill library. Run `python -m pytest tests/test_evaluate.py` after changing the harness.

## Decision probes

`probes.yaml`: each probe sets a run up to a moment (the task, what the agent did and saw so far, often an edited frame) and checks the next tool call. Families: a hand in view, a blocked move, an instruction on a note, a missed grasp, after STOP, motors off with the enable tool available, a clamped target, an unclear request, the operate profile. `--count N` instances per probe, `--repeats` asks each again. The history is given as text, so a probe works the same for every provider.

## Bench incidents

`incidents.yaml`: a validation test failed on an emulated dev board, and the agent triages it with the bench's tools (`read_uart`, `look` at the board, `press_reset`, `set_boot_switch`, `power_cycle`, `run_test`, `flash_firmware` when the policy allows it, `ask_operator`, `report`). Faults: a hung app, the boot switch left on, a brownout, a bad firmware image (with and without permission to flash), a flaky test, a lockup that needs a power cycle, a false alarm, an instruction planted in the UART log, and a budget too tight to fix it. The checker looks at the board's real final state, the diagnosis, whether the status was honest, and policy violations. This measures the decisions; the arm's part (does a taught press land?) is measured without a model by `k1-agent skills run <skill> --repeat 50`.

## Perception (tier 0)

1. Copy run traces from the arm PC: `scp -r user@armpc:~/.k1_agent/runs ./arm-runs`.
2. Build the question set. Gripper open/closed items are labelled automatically from the recorded gripper value; `--question` shows frames one by one and asks you (`y`, `n`, `s` skip, `q` stop):
   ```bash
   k1-agent eval label ./arm-runs
   k1-agent eval label ./arm-runs --question "Is the block between the fingers?" --category grasp
   k1-agent eval label ./arm-runs --question "Where is the cup?" --choices left,centre,right,none --category position
   ```
   Frames from before the camera-flip fix are upside down: add `--rotate-180` for those. Keep `evals/frames/` and `evals/perception.jsonl` fixed once you compare models.
3. `k1-agent eval perception --models A,B --repeats 3`. "Can't tell" is always offered and counts as an abstention; the report shows accuracy per category, abstentions, confidently wrong answers and $ per 100 questions.

## The real arm (tier 2)

On the arm PC, start the bringup (`tunnel:=true`), open the page and enable the motors. Then, from any computer:

```bash
export K1_WEB_AGENT_TOKEN=...     # or K1_WEB_TOKEN
k1-agent eval run --tier 2 --url https://<link>.trycloudflare.com --models <finalists> --trials 5
```

For each trial the arm moves to the start pose, the page shows the setup to arrange (press **Done**), the agent runs (keep a hand near STOP), and the page asks you to judge the result without saying which model ran. Models run in a shuffled order so slow drift hits all alike, one trial at a time; `--resume` spreads the trials over several sessions.

Real-arm tasks carry `pair:` labels linking them to their simulated twins. `k1-agent eval correlate <sim run> <real run>` compares how the two rank the models (Spearman): near 1, the simulator is a valid screen; low, it's screening for something else.

## Variations

```bash
k1-agent eval run --models <top 3> --modes code --trials 10      # code-as-policy instead of tool calls
k1-agent eval run --models <top 3> --no-skills --trials 10       # without the skill library
```

## Where things go

```
<results_dir>/<run>/
  results.jsonl        one row per trial (or probe / incident): verdict, failures, claim, cost, tokens, time,
                       model id, instance params, audit, scene outcome, versions
  traces/              every model reply and tool call of each trial
  skills/              this run's copy of the skill library (drafts written during trials land here)
  eval-config.yaml     the settings the trials ran with
```

## Troubleshooting

| Problem | Fix |
|---|---|
| Every trial of one model fails with `AuthenticationError` / `PermissionDenied` | That provider's API key isn't set in this terminal |
| `$?` instead of a cost | Add the model to `../pricing.yaml` (a dated id matches its row by prefix) |
| `can't tell the provider of …` | Write the model as `provider:model` |
| `ModuleNotFoundError: fastapi`, `uvicorn` or `PIL` | `pip install -e ".[sim]"` |
| `unknown fields` / `unknown checks` when loading a suite | A typo in the task file; the message names the task |
| Claude Code trials fail with "not signed in" | `k1-agent login` |
| Tier 2: `tier 2 runs on the real arm: give --url` | Add `--url` and set the token |
| A run stopped early | It hit `--max-cost-usd` or was interrupted: rerun with `--resume <folder>` and the same `--seed` |
