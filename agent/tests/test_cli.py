import json
import os

from k1_agent.cli import DEFAULT_CONFIG, load_config, main


def test_default_config_and_seed_skills():
    config = load_config(None)
    assert config["provider"] in config["models"]
    assert os.path.isabs(config["skills_dir"]) and os.path.isabs(config["runs_dir"])
    from k1_agent.skills import SkillLibrary
    names = [s["name"] for s in SkillLibrary(config["skills_dir"]).list()]
    assert "k1-basics" in names
    assert all(s["description"] for s in SkillLibrary(config["skills_dir"]).list())


def test_observe_and_skills(live_server, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("K1_WEB_TOKEN", live_server["token"])
    config = tmp_path / "config.yaml"
    config.write_text(open(DEFAULT_CONFIG).read().replace("skills_dir: skills", f"skills_dir: {tmp_path / 'skills'}"))
    out = tmp_path / "frame.jpg"

    assert main(["--config", str(config), "--url", live_server["url"], "observe", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed[:printed.rindex("}") + 1])["ok"] and out.read_bytes().startswith(b"\xff\xd8")

    assert main(["--config", str(config), "skills"]) == 0
    assert "(no skills yet)" in capsys.readouterr().out
    assert main(["--config", str(config), "skills", "approve", "missing"]) == 1


def test_bad_token(live_server, monkeypatch, capsys):
    monkeypatch.setenv("K1_WEB_TOKEN", "wrong")
    assert main(["--url", live_server["url"], "observe"]) == 1
    assert "could not connect" in capsys.readouterr().err
