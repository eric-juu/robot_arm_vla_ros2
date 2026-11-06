import io
import json
import os

import pytest
from PIL import Image

from k1_agent.cli import DEFAULT_CONFIG, main
from k1_agent.client import K1Client
from k1_agent.primitives import Primitives
from k1_agent.skills import SkillLibrary
from k1_agent.tools import PROGRAM_EYES, ArmToolbox
from k1_agent.trace import Trace
from conftest import motors_on

SWEEP = "def run(k1):\n    r = k1.move_joint_delta('shoulder_pan', 0.1)\n    return r['ok']\n"


@pytest.fixture
def k1(live_server):
    with K1Client(live_server["url"], live_server["token"]) as client:
        motors_on(live_server, client)
        yield client


def test_schemas_come_from_the_signatures():
    schemas = {name: ArmToolbox.schema(name) for name in ArmToolbox.all_tools()}
    assert schemas["move_joint_delta"]["properties"]["joint"]["enum"][:2] == ["shoulder_pan", "shoulder_lift"]
    gripper = schemas["set_gripper"]["properties"]["value"]
    assert (gripper["minimum"], gripper["maximum"]) == (0.0, 1.0)
    positions = schemas["move_joints"]["properties"]["positions"]
    assert any(option.get("minItems") == 6 for option in positions["anyOf"])
    assert schemas["done"]["required"] == ["success", "summary"]
    assert len(ArmToolbox.schema_digest()) == 10


def test_programs_measure_the_camera_and_models_see_the_same_tools(k1, live_server, monkeypatch):
    img = Image.new("RGB", (100, 80), (90, 90, 90))
    img.paste((230, 200, 40), (10, 20, 30, 40))
    img.paste((230, 200, 40), (60, 50, 70, 60))
    buffer = io.BytesIO()
    img.save(buffer, "JPEG", quality=95)
    monkeypatch.setattr(k1, "snapshot", lambda: buffer.getvalue())

    trace = Trace(live_server["runs"], "eyes")
    box = ArmToolbox(Primitives(k1, trace), None)
    assert not set(PROGRAM_EYES) & set(box.available()) and set(PROGRAM_EYES) <= set(box.program_api())
    code = ("def run(k1):\n"
            "    blobs = k1.find_color('yellow')['blobs']\n"
            "    box = k1.gray_region(15, 25, 75, 55)\n"
            "    return [blobs, box['width'], box['height'], len(box['pixels']), box['pixels'][0],\n"
            "            k1.find_color('mauve')['error'], k1.gray_region(200, 0, 300, 10)['error']]\n")
    result = box.run_program(code)
    assert result.ok, result.error
    blobs, width, height, count, first, unknown, outside = result.returned
    assert [(b["left"], b["right"], b["top"], b["bottom"]) for b in blobs] == [(10, 29, 20, 39), (60, 69, 50, 59)]
    assert blobs[0]["pixels"] == pytest.approx(400, abs=10) and blobs[0]["x"] == pytest.approx(19.5, abs=0.5)
    assert (width, height, count) == (60, 30, 1800) and first > 150
    assert (unknown, outside) == ("bad_arguments", "bad_arguments")
    assert not any("pixels" in e["result"] for e in trace.entries)      # the frame is saved instead
    assert [e["frame"] for e in trace.entries if e["name"] == "gray_region"][0]


def test_bad_calls_get_typed_errors(k1):
    toolbox = ArmToolbox(Primitives(k1, allow_motion=True), None)
    assert toolbox.call("fly", {})[0]["error"] == "unknown_tool"
    assert toolbox.call("set_gripper", {"value": 3})[0]["error"] == "bad_arguments"
    assert toolbox.call("move_joint_delta", {"joint": "elbow"})[0]["error"] == "bad_arguments"
    assert toolbox.call("read_skill", {"name": "x"})[0]["error"] == "unavailable"     # no skill library
    assert "read_skill" not in toolbox.available() and "enable" not in toolbox.available()


def test_code_skills_are_drafted_approved_and_run(k1, live_server, tmp_path):
    skills = SkillLibrary(str(tmp_path / "skills"))
    with pytest.raises(ValueError):
        skills.save_code_draft("bad", "def run(k1):\n    import os\n")
    skills.save_code_draft("nudge-base", SWEEP, "turn the base a little")
    assert skills.pending() == [{"name": "nudge-base", "description": "turn the base a little", "code": True}]
    skills.approve("nudge-base")
    assert skills.read_code("nudge-base") == SWEEP and "nudge-base (code)" in skills.index_text()

    trace = Trace(live_server["runs"], "skill")
    teach = ArmToolbox(Primitives(k1, trace, allow_motion=True), skills)
    result, frame = teach.call("run_skill", {"name": "nudge-base"})
    assert result["ok"] and result["returned"] is True and frame
    assert result["joints"]["shoulder_pan"] == pytest.approx(0.1, abs=0.05)
    assert [e["name"] for e in trace.entries if e["kind"] == "call"][:1] == ["move_joint_delta"]

    # operate profile: the same program can't move joints freely
    operate = ArmToolbox(Primitives(k1, allow_motion=True), skills, profile="operate")
    result, _ = operate.call("run_skill", {"name": "nudge-base"})
    assert not result["ok"] and "move_joint_delta" in result["error"]


def test_skills_run_replays_without_a_model(k1, live_server, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("K1_WEB_TOKEN", live_server["token"])
    skills_dir = tmp_path / "skills"
    os.makedirs(skills_dir)
    (skills_dir / "nudge-base.md").write_text("---\ndescription: turn the base a little\n---\nRun it.\n")
    (skills_dir / "nudge-base.py").write_text(SWEEP)
    config = tmp_path / "config.yaml"
    config.write_text(open(DEFAULT_CONFIG).read()
                      .replace("skills_dir: skills", f"skills_dir: {json.dumps(str(skills_dir))}")
                      .replace("runs_dir: ~/.k1_agent/runs", f"runs_dir: {json.dumps(live_server['runs'])}"))
    base = ["--config", str(config), "--url", live_server["url"], "skills"]
    assert main(base + ["run", "nudge-base", "--repeat", "2", "--allow-motion", "--reset-pose", "home"]) == 0
    assert "nudge-base: 2/2 runs passed" in capsys.readouterr().out
    assert main(base + ["run", "nudge-base"]) == 1          # no --allow-motion: refused, so it fails
    assert main(base + ["run", "missing"]) == 1
