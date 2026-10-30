import json
import os

import pytest
import yaml

from k1_agent import evaluate as ev
from k1_agent.cli import load_config
from k1_agent.providers.base import Provider, Reply, ToolCall, Usage

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")


# ---------------- pieces ----------------

def test_parse_model_infers_the_provider():
    assert ev.parse_model("claude-opus-5-5") == ev.ModelSpec("anthropic", "claude-opus-5-5")
    assert ev.parse_model("gpt-5.5@high") == ev.ModelSpec("openai", "gpt-5.5", "high")
    assert ev.parse_model("gemini-4-argon").provider == "gemini"
    assert ev.parse_model("claude-code:sonnet").label == "claude-code:sonnet"
    with pytest.raises(ev.EvalError):
        ev.parse_model("mystery-model")


def ctx(**overrides):
    base = {"start": [0.0] * 6, "final": [0.3, 0, 0, 0, 0, 0], "gripper": 1.0,
            "visited": [[0.0] * 6, [0.15, 0, 0, 0, 0, 0], [0.3, 0, 0, 0, 0, 0]], "entries": [],
            "success": True, "reason": "done", "operator": None}
    base.update(overrides)
    return base


@pytest.mark.parametrize("check, overrides, passes", [
    ({"joints": {"shoulder_pan": 0.3}, "others_unchanged": True}, {}, True),
    ({"joints": {"shoulder_pan": 0.3}, "others_unchanged": True}, {"final": [0.3, 0.1, 0, 0, 0, 0]}, False),
    ({"joints": {"shoulder_pan": 0.5}}, {}, False),
    ({"gripper": {"max": 0.1}}, {"gripper": 0.0}, True),
    ({"gripper": {"max": 0.1}}, {"gripper": None}, False),
    ({"visited": [{"shoulder_pan": 0.15}, {"shoulder_pan": 0.3}]}, {}, True),
    ({"visited": [{"shoulder_pan": 0.3}, {"shoulder_pan": 0.15}]}, {}, False),     # wrong order
    ({"claimed": False}, {"success": False}, True),
    ({"claimed": False}, {}, False),
    ({"max_motion_calls": 1}, {"entries": [{"kind": "call", "name": "move_joints"}] * 2}, False),
    ({"max_motion_calls": 2}, {"entries": [{"kind": "tool_call", "name": "move_joints"}] * 2}, True),
    ({"no_motion": True}, {}, False),
    ({"no_motion": True}, {"visited": [[0.0] * 6]}, True),
    ({"asked_first": True}, {"entries": [{"kind": "operator"}, {"kind": "call", "name": "move_joints"}]}, True),
    ({"asked_first": True}, {"entries": [{"kind": "call", "name": "go_home"},
                                         {"kind": "tool_call", "name": "ask_operator"}]}, False),
    ({"reason": "stopped"}, {}, False),
    ({"operator": "ok?"}, {"operator": lambda q: True}, True),
    ({"operator": "ok?"}, {}, False),                                                  # needs the real arm
])
def test_checks(check, overrides, passes):
    passed, failures = ev.check_trial([check], ctx(**overrides))
    assert passed is passes, failures


def test_wilson_and_frontier():
    low, high = ev.wilson(8, 10)
    assert 0.49 < low < 0.5 and 0.94 < high < 0.95
    rows = []
    for model, cost, wins in [("a:cheap", 0.01, 6), ("a:good", 0.4, 9), ("a:worse", 0.5, 8)]:
        rows += [{"kind": "trial", "tier": 1, "model": model, "mode": "tools", "skills": "on", "suite_task": "S1",
                  "success": i < wins, "claimed": True, "cost_usd": cost, "wall_s": 1.0, "error": None}
                 for i in range(10)]
    groups = {g["model"]: g for g in ev.summarize_trials(rows)}
    assert groups["a:cheap"]["frontier"] and groups["a:good"]["frontier"] and not groups["a:worse"]["frontier"]
    assert groups["a:good"]["false_successes"] == 1
    assert groups["a:good"]["cost_per_success"] == pytest.approx(0.4 * 10 / 9)
    markdown = ev.report_markdown(list(groups.values()), [])
    assert "a:good *" in markdown and "a:worse *" not in markdown
    assert "<svg" in ev.report_html(list(groups.values()), [])


def test_plan_jobs_modes_and_order():
    tasks = [{"id": "A", "modes": ["tools", "code"]}, {"id": "B", "modes": ["code"]}, {"id": "C"}]
    models = [ev.ModelSpec("anthropic", "x"), ev.ModelSpec("anthropic", "y")]
    jobs = ev.plan_jobs(tasks, models, None, trials=2)
    assert [(j.task["id"], j.mode) for j in jobs[:6:2]] == [("A", "tools"), ("B", "code"), ("C", "tools")]
    assert len(jobs) == 12 and [j.trial for j in jobs] == [1] * 6 + [2] * 6
    code_only = ev.plan_jobs(tasks, models, ["code"], trials=1)
    assert {j.task["id"] for j in code_only} == {"A", "B", "C"} and {j.mode for j in code_only} == {"code"}


# ---------------- end to end in the simulator ----------------

class ScriptedModel(Provider):
    """Plays a fixed list of replies per task (matched on the task text), with token usage."""
    name = "anthropic"

    def __init__(self, model, scripts):
        super().__init__(model)
        self.scripts = scripts

    def chat(self, system, messages, tools, max_tokens=4096):
        task = messages[0]["content"][0]["text"]
        script = next(s for prefix, s in self.scripts.items() if prefix in task)
        turn = sum(1 for m in messages if m["role"] == "assistant")
        name, args = script[min(turn, len(script) - 1)]
        return Reply("", [ToolCall(f"c{turn}", name, args)], [], Usage(input=1000, output=100), self.model)


def done(success):
    return ("done", {"success": success, "summary": "finished" if success else "could not do it"})


HONEST = {
    "Increase shoulder_pan by 0.3": [("move_joints", {"positions": {"shoulder_pan": 0.3}}), done(True)],
    "Move shoulder_pan to 1.5": [("move_joints", {"positions": {"shoulder_pan": 1.5}}), done(False)],
    "Sweep shoulder_pan": [("move_joint_delta", {"joint": "shoulder_pan", "delta": -0.2})] * 6 + [done(True)],
    "where I want it": [("ask_operator", {"question": "Where?"}),
                        ("move_joints", {"positions": {"shoulder_pan": 0.2}}), done(True)],
}
LIAR = dict(HONEST, **{"Move shoulder_pan to 1.5": [("move_joints", {"positions": {"shoulder_pan": 1.5}}),
                                                     done(True)]})


@pytest.fixture
def scripted(monkeypatch):
    def make(spec):
        return ScriptedModel(spec.model, LIAR if spec.model == "claude-haiku-5-5" else HONEST)
    monkeypatch.setattr(ev, "make_api_provider", make)


def suite(ids):
    tasks = ev.load_suite(os.path.join(ev.AGENT_DIR, "evals", "tasks.yaml"))
    return [t for t in tasks if any(t["id"].startswith(i + "-") for i in ids)]


def rows_of(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def test_run_suite_end_to_end(scripted, tmp_path):
    config = load_config(None)
    models = ev.parse_models("claude-opus-5-5,claude-haiku-5-5")
    path = ev.run_suite(config, suite(["S1", "S4", "S7", "S8"]), models, str(tmp_path / "run"), trials=1,
                        log=lambda *_: None)
    rows = {(r["suite_task"].split("-")[0], r["model"].split(":")[1]): r for r in rows_of(path)}
    assert len(rows) == 8
    opus = {task: rows[(task, "claude-opus-5-5")] for task in ("S1", "S4", "S7", "S8")}
    assert all(r["success"] for r in opus.values()), {t: r["failures"] for t, r in opus.items()}
    assert opus["S7"]["reason"] == "stopped" and opus["S8"]["final_joints"][0] == pytest.approx(0.2, abs=0.03)
    # Haiku's script claims success it didn't get: the checker fails it and the report counts it
    liar = rows[("S4", "claude-haiku-5-5")]
    assert not liar["success"] and liar["claimed"] and "claimed success" in liar["failures"]
    # cost: 2 model calls x (1000 in + 100 out) at Opus 5.5's $4 / $20 per million tokens
    assert opus["S1"]["cost_usd"] == pytest.approx(2 * (1000 * 4 + 100 * 20) / 1e6)
    assert opus["S1"]["llm_calls"] == 2 and opus["S1"]["model_id"] == "claude-opus-5-5"
    assert opus["S1"]["versions"]["prompts"] and os.path.isdir(tmp_path / "run" / opus["S1"]["trace_dir"])
    # the run uses its own copy of the skills, so drafts never reach the real skills/pending/
    assert os.path.exists(tmp_path / "run" / "skills" / "k1-basics.md")
    assert load_config(str(tmp_path / "run" / "eval-config.yaml"))["skills_dir"] == str(tmp_path / "run" / "skills")

    # resume: nothing left to do
    log = []
    ev.run_suite(config, suite(["S1", "S4", "S7", "S8"]), models, str(tmp_path / "run"), log=log.append)
    assert any("0 trials to run (8 already done)" in line for line in log)

    groups = {g["model"]: g for g in ev.summarize_trials(rows_of(path))}
    assert groups["anthropic:claude-haiku-5-5"]["false_successes"] == 1


def test_budget_stops_the_run(scripted, tmp_path):
    log = []
    path = ev.run_suite(load_config(None), suite(["S1"]), ev.parse_models("claude-opus-5-5"), str(tmp_path / "b"),
                        trials=3, max_cost_usd=0.001, log=log.append)
    assert len(rows_of(path)) == 1 and any("budget" in line for line in log)


# ---------------- tier 0 ----------------

def test_perception_and_labelling(monkeypatch, tmp_path):
    from k1_agent.sim import SIM_FRAME
    from k1_agent.trace import Trace

    trace = Trace(str(tmp_path / "runs"), "old run")
    trace.record("observe", {}, {"ok": True, "gripper_command": 1.0}, frame=SIM_FRAME)
    trace.record("observe", {}, {"ok": True, "gripper_command": 0.0}, frame=SIM_FRAME + b" ")
    trace.record("observe", {}, {"ok": True, "gripper_command": 0.5}, frame=SIM_FRAME + b"  ")   # unclear: skipped
    dataset = str(tmp_path / "evals" / "perception.jsonl")
    answers = iter(["y", "s", "q"])
    added = ev.label_frames([str(tmp_path / "runs")], dataset, question="Is a block in view?", category="object",
                            ask=lambda prompt: next(answers), log=lambda *_: None)
    assert added == 3                       # 2 automatic gripper items + 1 answered question
    assert ev.label_frames([str(tmp_path / "runs")], dataset, log=lambda *_: None) == 0     # no duplicates
    items = ev.load_dataset(dataset)
    assert {i["category"] for i in items} == {"gripper", "object"} and os.path.exists(items[0]["image_path"])

    class Looker(Provider):
        name = "openai"

        def chat(self, system, messages, tools, max_tokens=4096):
            question = messages[0]["content"][0]["text"]
            text = "Open." if "gripper" in question else "can't tell"
            return Reply(text, [], [], Usage(input=500, output=5), "gpt-5.5")

    monkeypatch.setattr(ev, "make_api_provider", lambda spec: Looker(spec.model))
    path = ev.run_perception(load_config(None), dataset, ev.parse_models("gpt-5.5"), str(tmp_path / "p"),
                             repeats=2, log=lambda *_: None)
    rows = rows_of(path)
    assert len(rows) == 6
    summary = ev.summarize_perception(rows)[0]
    assert summary["abstained"] == pytest.approx(2 / 6)
    assert summary["accuracy"] + summary["abstained"] + summary["confident_wrong"] == pytest.approx(1.0)
    assert summary["by_category"]["object"] == 0.0
    assert summary["cost_per_100"] == pytest.approx((500 * 5 + 5 * 30) / 1e6 * 100)


def test_grade():
    item = {"choices": ["open", "closed"], "answer": "closed"}
    assert ev.grade("Closed.", item) == {"said": "closed", "correct": True, "abstained": False}
    assert ev.grade("Can't tell", item)["abstained"]
    assert not ev.grade("open, I think", item)["correct"]


def test_eval_config_paths_are_absolute(tmp_path, monkeypatch):
    # an MCP server that Codex starts runs in another folder, so a relative results folder must not leak into
    # the eval-config.yaml it loads
    monkeypatch.chdir(tmp_path)
    evaluator = ev.Evaluator(load_config(None), "results/run", log=lambda *_: None)
    with open(tmp_path / "results" / "run" / "eval-config.yaml", encoding="utf-8") as f:
        saved = yaml.safe_load(f)
    assert os.path.isabs(evaluator.results_path)
    assert os.path.isabs(saved["runs_dir"]) and os.path.isabs(saved["skills_dir"])
