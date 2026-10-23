"""
MCP server exposing the K1 primitives as tools, so a coding agent (Claude Code, Codex, ...) can be
the agent, the way ASPIRE's System 2 drives its robot. The operating rules for that agent are in
agent/AGENTS.md.

    claude mcp add k1 -- k1-agent mcp --allow-motion

The same interlocks as `k1-agent run` apply: without --allow-motion every motion tool refuses.
"""
import json

from k1_agent.primitives import Primitives
from k1_agent.skills import SkillLibrary

try:                                    # mcp >= 2
    from mcp.server.mcpserver import Image, MCPServer as Server
except ImportError:                     # mcp 1.x
    from mcp.server.fastmcp import FastMCP as Server, Image

INSTRUCTIONS = """Tools for a real Unitree K1 robot arm (6 joints in radians + gripper 1 open / 0 closed) with
a wrist camera. Call get_capability_graph and observe first. Move in small steps, check a fresh frame after
every move, and call stop if anything looks wrong. Read agent/AGENTS.md for the operating rules."""


def text(result: dict) -> str:
    return json.dumps({k: v for k, v in result.items() if k != "image"}, default=str)


def with_frame(prims: Primitives, result: dict) -> list:
    """Tool output: the result as JSON text plus the camera frame taken after the call."""
    frame = result.get("image") or prims.last_frame
    return [text(result)] + ([Image(data=frame, format="jpeg")] if frame else [])


def build_server(prims: Primitives, skills: SkillLibrary, operator=None):
    """`operator` (e.g. WebOperator) adds an ask_operator tool; leave it out when a human is
    already chatting with the agent, as in an interactive Claude Code session."""
    server = Server("k1-arm", instructions=INSTRUCTIONS)

    @server.tool()
    def get_capability_graph() -> str:
        """What this session can do: the tools, interlocks, step budget and saved poses."""
        state = prims.client.state
        return json.dumps({
            "robot": "Unitree K1, 6-DoF arm + gripper, wrist camera (no depth)",
            "tools": ["observe", "move_joints", "move_joint_delta", "go_pose", "go_home", "set_gripper", "stop",
                      "wait", "list_skills", "read_skill", "save_skill_draft", "note"] +
                     (["ask_operator"] if operator is not None else []) +
                     (["enable"] if prims.allow_enable else []),
            "allow_motion": prims.allow_motion,
            "allow_enable": prims.allow_enable,
            "steps_left": prims.max_steps - prims.steps_used,
            "max_step_rad": prims.max_step_rad,
            "motors_ready": state.get("ready"),
            "poses": state.get("poses", []),
        })

    @server.tool()
    def observe() -> list:
        """Arm state (joints in rad, gripper, limits, poses) and a fresh wrist-camera image."""
        return with_frame(prims, prims.observe())

    @server.tool()
    def move_joints(positions: dict[str, float] | list[float], timeout: float = 10.0) -> list:
        """Move to absolute joint angles (rad): 6 numbers in joint order, or {joint: angle} for some joints."""
        return with_frame(prims, prims.move_joints(positions, timeout))

    @server.tool()
    def move_joint_delta(joint: str, delta: float, timeout: float = 10.0) -> list:
        """Move one joint by a small relative amount in rad (capped per call)."""
        return with_frame(prims, prims.move_joint_delta(joint, delta, timeout))

    @server.tool()
    def go_pose(name: str, timeout: float = 10.0) -> list:
        """Move to a saved, named pose."""
        return with_frame(prims, prims.go_pose(name, timeout))

    @server.tool()
    def go_home() -> list:
        """Move to the 'home' pose."""
        return with_frame(prims, prims.go_home())

    @server.tool()
    def set_gripper(value: float) -> list:
        """Gripper: 1.0 fully open, 0.0 closed."""
        return with_frame(prims, prims.set_gripper(value))

    @server.tool()
    def stop() -> str:
        """Hold the current position immediately."""
        return text(prims.stop())

    @server.tool()
    def wait(seconds: float) -> str:
        """Pause for up to 10 seconds."""
        return text(prims.wait(seconds))

    @server.tool()
    def note(message: str) -> str:
        """Show a line in the web page's agent log, so the operator can follow along."""
        return text(prims.note(message))

    @server.tool()
    def list_skills() -> str:
        """Names and descriptions of the skills in the library."""
        return skills.index_text()

    @server.tool()
    def read_skill(name: str) -> str:
        """Full text of one skill."""
        return skills.read(name) or f"no skill {name!r}; available:\n{skills.index_text()}"

    @server.tool()
    def save_skill_draft(name: str, markdown: str) -> str:
        """Propose a new skill (markdown with name/description front matter). A human approves it later."""
        try:
            return f"saved for review: {skills.save_draft(name, markdown)}"
        except ValueError as e:
            return f"not saved: {e}"

    if operator is not None:
        @server.tool()
        def ask_operator(question: str) -> str:
            """Ask the human operator (through the web page) and wait for the answer, e.g. to place or reset
            an object, or when unsure whether something is safe."""
            return operator.ask(question)

    if prims.allow_enable:
        @server.tool()
        def enable(value: bool) -> str:
            """Lock (true) or release (false) the motors. Releasing makes the arm fall."""
            return text(prims.enable(value))

    return server
