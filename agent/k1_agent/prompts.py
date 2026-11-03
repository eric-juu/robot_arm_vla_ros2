"""
The agent's instructions, in one place. Every runtime gets the same operating rules (agent/AGENTS.md):
the agent's own loop adds a brief of the session it starts with, Claude Code and Codex read them as their
system prompt / MCP instructions and look the rest up with get_capability_graph.
"""
import json
import os

from k1_agent.primitives import ARM_JOINTS

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES_PATH = os.path.join(AGENT_DIR, "AGENTS.md")

CLI_NOTE = "\nYou have no shell or file tools here; use only the k1 tools."

CODE_INSTRUCTIONS = """Write a Python program that does the task. Define `run(k1)`; it may only use these
primitives on `k1` (and `import math`). Every call returns a dict with "ok"; check it and react to failures
(fields "error" and "message"). observe() returns joints and state; it cannot show you the image inside the
program, so structure the program as moves plus checks of joint values, and you will see the final frame after
it runs.

{api}

Reply with a short plan, then the whole program in one ```python block."""


def operating_rules() -> str:
    try:
        with open(RULES_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "Move the arm in small steps, look after every move, and call done(success, summary) at the end."


def session_brief(toolbox, obs: dict) -> str:
    limits = ", ".join(f"{n} [{a:.2f}, {b:.2f}]" for n, (a, b) in obs.get("limits", {}).items()) or "unknown"
    skills = toolbox.skills.index_text() if getattr(toolbox, "skills", None) else "(no skill library)"
    prims = toolbox.prims
    return "\n".join([
        "## This session",
        f"- Joints, in order: {', '.join(ARM_JOINTS)} (radians). Gripper: 1.0 fully open, 0.0 closed.",
        f"- Joint limits: {limits}",
        f"- Saved poses: {', '.join(obs.get('poses', [])) or 'none'}",
        f"- Profile: {toolbox.profile} ({'free joint motion allowed' if toolbox.profile == 'teach' else 'taught poses, gripper and code skills only'})",
        f"- Motion {'allowed' if prims.allow_motion else 'refused (dry run)'}; "
        f"{prims.max_steps - prims.steps_used} motion steps left; move_joint_delta capped at {prims.max_step_rad} rad.",
        "- The only camera is on the wrist, next to the gripper. There is no depth sensor: judge distance from "
        "apparent size and from how the view changes as you move.",
        "- The initial observation is in the first message, so you can skip get_capability_graph and observe at "
        "the start.",
        "",
        "Skill library:",
        skills,
    ])


def system_prompt(toolbox, obs: dict) -> str:
    return operating_rules() + "\n\n" + session_brief(toolbox, obs)


def api_doc(toolbox) -> str:
    """The code-mode API, from the primitives' signatures, for the names this session allows."""
    import inspect

    from k1_agent.primitives import Primitives

    lines = []
    for name in toolbox.program_api():
        fn = getattr(Primitives, name)
        params = [p for p in inspect.signature(fn).parameters.values() if p.name != "self"]
        doc = (inspect.getdoc(fn) or "").splitlines()[0] if inspect.getdoc(fn) else ""
        lines.append(f"- k1.{name}({', '.join(str(p) for p in params)}): {doc}")
    return "\n".join(lines)


def prompt_digest() -> str:
    """What the instructions depend on, for the eval's version record."""
    import hashlib

    from k1_agent.tools import ArmToolbox

    text = json.dumps([operating_rules(), CODE_INSTRUCTIONS, CLI_NOTE, ArmToolbox.schema_digest()])
    return hashlib.sha1(text.encode()).hexdigest()[:10]
