import asyncio
import json

import pytest

pytest.importorskip("mcp")

from k1_agent.client import K1Client  # noqa: E402
from k1_agent.mcp_server import build_server  # noqa: E402
from k1_agent.primitives import Primitives  # noqa: E402
from k1_agent.skills import SkillLibrary  # noqa: E402
from k1_agent.tools import ArmToolbox  # noqa: E402


def contents(result):
    """call_tool returns content blocks, or (blocks, structured) depending on the SDK version."""
    if isinstance(result, tuple):
        result = result[0]
    return getattr(result, "content", result)


def input_schema(tool) -> dict:
    """mcp 2.x calls it input_schema, 1.x inputSchema."""
    return getattr(tool, "input_schema", None) or getattr(tool, "inputSchema")


def test_tools_over_mcp(live_server, tmp_path):
    with K1Client(live_server["url"], live_server["token"]) as k1:
        toolbox = ArmToolbox(Primitives(k1, allow_motion=False), SkillLibrary(str(tmp_path / "skills")))
        server = build_server(toolbox)

        async def scenario():
            listed = {t.name: t for t in await server.list_tools()}
            assert set(listed) == set(toolbox.available())
            assert {"observe", "move_joints", "get_capability_graph", "save_skill_draft", "done"} <= set(listed)
            assert "enable" not in listed and "ask_operator" not in listed

            # the MCP SDK builds the same schema the agent's own loop sends to API models
            for spec in toolbox.specs():
                schema = input_schema(listed[spec.name])
                assert set(schema.get("properties", {})) == set(spec.parameters.get("properties", {})), spec.name
                assert set(schema.get("required", [])) == set(spec.parameters.get("required", [])), spec.name
            assert input_schema(listed["move_joint_delta"])["properties"]["joint"]["enum"][0] == "shoulder_pan"

            blocks = contents(await server.call_tool("observe", {}))
            assert json.loads(blocks[0].text)["ok"] and blocks[1].type == "image"

            blocks = contents(await server.call_tool("move_joints", {"positions": {"shoulder_pan": 0.2}}))
            assert json.loads(blocks[0].text)["error"] == "safety_interlock"

            graph = json.loads(contents(await server.call_tool("get_capability_graph", {}))[0].text)
            assert graph["allow_motion"] is False and graph["profile"] == "teach"

            saved = json.loads(contents(await server.call_tool("save_skill_draft", {
                "name": "look-around", "markdown": "---\ndescription: scan\n---\nPan slowly."}))[0].text)
            assert saved["ok"] and saved["saved_for_review"].endswith("look-around.md")

            done = json.loads(contents(await server.call_tool("done", {"success": False, "summary": "dry"}))[0].text)
            assert done["ok"] and toolbox.outcome == (False, "dry")

        asyncio.run(scenario())


def test_operate_profile_has_no_free_motion(live_server, tmp_path):
    with K1Client(live_server["url"], live_server["token"]) as k1:
        toolbox = ArmToolbox(Primitives(k1, allow_motion=True), SkillLibrary(str(tmp_path / "skills")),
                             profile="operate")
        names = {t.name for t in asyncio.run(build_server(toolbox).list_tools())}
        assert {"go_pose", "set_gripper", "run_skill"} <= names
        assert not names & {"move_joints", "move_joint_delta", "save_pose"}
        assert "move_joints" not in toolbox.program_api()
        result, _ = toolbox.call("move_joints", {"positions": [0.0] * 6})
        assert result["error"] == "unavailable"
