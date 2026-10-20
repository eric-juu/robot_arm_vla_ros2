import pytest

from k1_agent.client import StoppedByOperator
from k1_agent.sandbox import check_program, run_program

API = ["observe", "move_joints", "wait"]


class FakeArm:
    def __init__(self, stop_on=None):
        self.calls = []
        self.stop_on = stop_on

    def dispatch(self, name, args, kwargs):
        self.calls.append((name, args, kwargs))
        if name == self.stop_on:
            raise StoppedByOperator()
        if name == "observe":
            return {"ok": True, "joints": {"shoulder_pan": 0.0}, "image": b"\xff\xd8"}
        return {"ok": True}


def test_runs_program_through_proxy():
    arm = FakeArm()
    code = (
        "import math\n"
        "def run(k1):\n"
        "    obs = k1.observe()\n"
        "    print('image:', obs['image'])\n"
        "    k1.move_joints([math.pi / 10] * 6, timeout=3)\n"
        "    return obs['joints']\n"
    )
    result = run_program(code, arm.dispatch, API, timeout=20)
    assert result.ok and result.returned == {"shoulder_pan": 0.0} and result.calls == 2
    assert result.output == ["image: <frame saved in trace>"]
    assert arm.calls[1] == ("move_joints", [[0.3141592653589793] * 6], {"timeout": 3})


def test_rejects_unsafe_programs():
    assert "cannot import" in check_program("import os\ndef run(k1): pass")
    assert "cannot import" in check_program("from subprocess import run\ndef run(k1): pass")
    assert "'_'" in check_program("def run(k1):\n    return ().__class__")
    assert "__" in check_program("def run(k1):\n    return __builtins__")
    assert "run(k1)" in check_program("x = 1")
    assert "syntax" in check_program("def run(k1) pass")
    assert not run_program("import os\ndef run(k1): pass", FakeArm().dispatch, API).ok


def test_runtime_errors_timeouts_and_restricted_builtins():
    result = run_program("def run(k1):\n    open('x', 'w')\n", FakeArm().dispatch, API, timeout=20)
    assert not result.ok and "NameError" in result.error and "program.py" in result.error

    result = run_program("def run(k1):\n    k1.teleport()\n", FakeArm().dispatch, API, timeout=20)
    assert not result.ok and "teleport" in result.error

    result = run_program("def run(k1):\n    while True:\n        pass\n", FakeArm().dispatch, API, timeout=2)
    assert not result.ok and "timed out" in result.error


def test_stop_propagates():
    code = ("def run(k1):\n"
            "    try:\n"
            "        k1.wait(5)\n"
            "    except Exception:\n"
            "        pass\n"
            "    k1.move_joints([0] * 6)\n")
    arm = FakeArm(stop_on="wait")
    with pytest.raises(StoppedByOperator):
        run_program(code, arm.dispatch, API, timeout=20)
    assert [c[0] for c in arm.calls] == ["wait"]        # nothing ran after STOP
