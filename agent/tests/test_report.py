import math
import random

import pytest

from k1_agent import report, taskgen


def test_pass_hat_k():
    # 9 of 10 on each of two tasks: C(9,3)/C(10,3) = 0.7
    assert report.pass_hat_k({"a": (9, 10), "b": (9, 10)}, 3) == pytest.approx(0.7)
    assert report.pass_hat_k({"a": (10, 10)}, 5) == 1.0 and report.pass_hat_k({"a": (4, 10)}, 5) == 0.0
    assert report.pass_hat_k({"a": (1, 1)}, 2) is None


def test_task_bootstrap_is_wider_than_wilson_when_tasks_differ():
    per_task = {"easy1": (10, 10), "easy2": (10, 10), "hard1": (0, 10), "hard2": (1, 10)}
    low, high = report.task_bootstrap(per_task)
    wl, wh = report.wilson(21, 40)
    assert low < wl and high > wh                     # trial noise understates task-to-task variation


def test_paired_delta():
    a = {"t1": (9, 10), "t2": (8, 10), "t3": (10, 10)}
    b = {"t1": (5, 10), "t2": (4, 10), "t3": (6, 10), "only_b": (0, 10)}
    point, low, high = report.paired_delta(a, b)
    assert point == pytest.approx(0.4) and 0 < low <= point <= high
    assert math.isnan(report.paired_delta({"x": (1, 1)}, {"y": (1, 1)})[0])


def test_spearman_and_correlate():
    assert report.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert report.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert report.spearman([1, 2], [1, 2]) is None

    def rows(rates, pair):
        out = []
        for model, rate in rates.items():
            out += [{"kind": "trial", "model": model, "success": i < rate * 10, "pair": pair} for i in range(10)]
        return out

    sim = rows({"a": 0.9, "b": 0.6, "c": 0.3}, "center") + rows({"a": 0.1, "b": 0.1, "c": 0.9}, "unpaired")
    real = rows({"a": 0.8, "b": 0.5, "c": 0.2}, "center")
    result = report.correlate(sim, real)
    assert result["pairs"] == ["center"] and result["rho"] == pytest.approx(1.0)
    assert "Spearman rank correlation: 1.00" in report.correlation_markdown(result, "sim", "real")


def test_templates():
    rng = random.Random(1)
    names = taskgen.sample({"j": {"choice": ["a", "b"]}, "d": {"uniform": [0.1, 0.2], "round": 2}, "n": {"int": [1, 3]},
                            "twice": "=d * 2", "label": "{j}-{n}"}, rng)
    assert names["twice"] == pytest.approx(names["d"] * 2) and names["label"] == f"{names['j']}-{names['n']}"
    filled = taskgen.fill({"{j}": "=round(d + 1, 1)", "list": ["=n", "x {j}"], "keep": "{unknown}"}, names)
    assert filled[names["j"]] == round(names["d"] + 1, 1) and filled["keep"] == "{unknown}"
    assert taskgen.evaluate("1.0 if t > 0 else -1.0", {"t": -2}) == -1.0
    with pytest.raises(taskgen.TaskError):
        taskgen.evaluate("__import__('os')", {})
    with pytest.raises(taskgen.TaskError):
        taskgen.evaluate("missing + 1", {})

    task = {"id": "T", "tier": 1, "params": {"x": {"choice": [1, 2, 3, 4, 5, 6]}}, "prompt": "go {x}",
            "check": [{"joints": {"shoulder_pan": "=x / 10"}}]}
    one, again = taskgen.instance(task, 1, seed=7), taskgen.instance(task, 1, seed=7)
    assert one == again and one["prompt"] == f"go {one['params_used']['x']}"
    assert {taskgen.instance(task, t, seed=7)["prompt"] for t in range(1, 20)} != {one["prompt"]}
    assert taskgen.instance({"id": "S", "tier": 1, "prompt": "p", "check": []}, 3)["params_used"] == {}


def test_a_cycle_goes_by_trial_number_whatever_the_seed():
    task = {"id": "C", "tier": 1, "params": {"dy": {"cycle": [1, 2, 3]}, "twice": "=dy * 2"}, "prompt": "{twice}",
            "check": []}
    assert [taskgen.instance(task, t, seed=t * 7)["params_used"]["dy"] for t in range(1, 8)] == [1, 2, 3, 1, 2, 3, 1]
    assert taskgen.instance(task, 2)["prompt"] == "4"


def test_lists_waypoints_and_levels():
    rng = random.Random(4)
    names = taskgen.sample({"n": 12, "codes": {"int": [10, 12], "count": "=n"},
                            "tour": {"waypoints": "=n", "joints": ["shoulder_pan", "wrist_flex"], "values": [-0.2, 0.0, 0.2]},
                            "text": "=numbered(tour)", "size": "=len(codes)"}, rng)
    assert len(names["codes"]) == 12 and names["size"] == 12
    assert all(a != b for a, b in zip(names["codes"], names["codes"][1:]))       # never the same twice in a row
    tour = names["tour"]
    assert all(v != 0.0 for v in tour[0].values())                                # the first pose is a real move
    assert all(a[j] != b[j] for a, b in zip(tour, tour[1:]) for j in a)           # every joint changes every step
    first = tour[0]
    assert names["text"].startswith(f"1) shoulder_pan {first['shoulder_pan']}, wrist_flex {first['wrist_flex']}; 2) ")
    with pytest.raises(taskgen.TaskError):
        taskgen.sample({"codes": {"int": [1, 9], "count": 0}}, rng)

    task = {"id": "L", "tier": 1, "levels": {"n": [2, 4]}, "params": {"twice": "=2 * n"}, "prompt": "{twice}",
            "check": [{"claimed": False}]}
    expanded = taskgen.expand_levels(task)
    assert [t["id"] for t in expanded] == ["L-n2", "L-n4"] and "levels" not in expanded[0]
    assert expanded[1]["level"] == {"name": "n", "value": 4, "group": "L"}
    assert taskgen.instance(expanded[1], 1)["prompt"] == "8"                      # the level is a param
    with pytest.raises(taskgen.TaskError):
        taskgen.expand_levels(dict(task, params={"n": 5}))                         # both a level and a param
    with pytest.raises(taskgen.TaskError):
        taskgen.expand_levels(dict(task, levels={"n": [2], "m": [3]}))


def test_horizon_estimate():
    # 100%, 80%, 40%, 0%: crosses 50% between 8 and 16, at 2 ** (3 + 0.3 / 0.4) on a log scale
    levels = [(4, 5, 5), (8, 4, 5), (16, 2, 5), (32, 0, 5)]
    assert report.horizon_estimate(levels) == pytest.approx(2 ** 3.75)
    low, high = report.horizon_interval(levels)
    assert low <= 2 ** 3.75 <= high and 4 <= low and high <= 32
    assert report.horizon_estimate([(4, 5, 5), (8, 3, 5)]) == math.inf         # never below 50%
    assert report.horizon_estimate([(4, 1, 5), (8, 0, 5)]) == 0.0              # below 50% from the start
    # a longer task that did better (noise) is pooled with the shorter one rather than read as a crossing
    assert report.horizon_estimate([(4, 2, 5), (8, 5, 5), (16, 0, 5)]) == pytest.approx(2 ** (3 + 0.2 / 0.7))


def test_horizon_report():
    def row(model, n, success, **extra):
        return {"kind": "trial", "model": model, "mode": "tools", "success": success, "claimed": success,
                "level": {"name": "n", "value": n, "group": "H1"}, "progress": 1.0 if success else 0.5,
                "cost_usd": 0.1, "llm_calls": n + 3, "reason": "done", **extra}

    rows = ([row("big", n, True) for n in (4, 8, 16) for _ in range(3)] +
            [row("small", 4, True)] * 3 + [row("small", 8, False, reason="max_turns")] * 2 + [row("small", 8, True)] +
            [row("small", 16, False, claimed=True)] * 3)
    assert report.ending(rows[-1]) == "false success" and report.ending(rows[-5]) == "out of budget"
    groups = report.summarize_horizons(rows)
    small = next(m for m in groups[0]["models"] if m["model"] == "small")
    assert small["levels"][8] == {"successes": 1, "n": 3, "progress": pytest.approx(2 / 3),
                                  "cost_per_success": pytest.approx(0.3), "turns": 11}
    text = report.horizon_markdown(rows)
    assert "## Long task H1: success · progress by n" in text and "| n=4 | n=8 | n=16 |" in text
    assert "> 16 [> 16, > 16]" in text                                          # big never fell below 50%
    assert "## Long tasks: how the runs ended" in text and "| small | tools | 44% | 33% | 0% | 22% |" in text
    assert report.horizon_markdown([{"kind": "trial", "model": "m", "success": True}]) == ""
