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
