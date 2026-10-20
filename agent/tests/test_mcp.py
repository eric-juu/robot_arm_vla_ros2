import asyncio
import json

import pytest

pytest.importorskip("mcp")

from k1_agent.client import K1Client  # noqa: E402
from k1_agent.mcp_server import build_server  # noqa: E402
from k1_agent.primitives import Primitives  # noqa: E402
from k1_agent.skills import SkillLibrary  # noqa: E402


def contents(result):
    """call_tool returns content blocks, or (blocks, structured) depending on the SDK version."""
    if isinstance(result, tuple):
        result = result[0]
    return getattr(result, "content", result)


def test_tools_over_mcp(live_server, tmp_path):
    with K1Client(live_server["url"], live_server["token"]) as k1:
        prims = Primitives(k1, allow_motion=False)
        server = build_server(prims, SkillLibrary(str(tmp_path / "skills")))

        async def scenario():
            names = {t.name for t in await server.list_tools()}
            assert {"observe", "move_joints", "get_capability_graph", "save_skill_draft"} <= names
            assert "enable" not in names

            blocks = contents(await server.call_tool("observe", {}))
            assert json.loads(blocks[0].text)["ok"] and blocks[1].type == "image"

            blocks = contents(await server.call_tool("move_joints", {"positions": {"shoulder_pan": 0.2}}))
            assert json.loads(blocks[0].text)["error"] == "safety_interlock"

            graph = json.loads(contents(await server.call_tool("get_capability_graph", {}))[0].text)
            assert graph["allow_motion"] is False

            saved = contents(await server.call_tool("save_skill_draft", {
                "name": "look-around", "markdown": "---\ndescription: scan\n---\nPan slowly."}))[0].text
            assert saved.startswith("saved for review")

        asyncio.run(scenario())
