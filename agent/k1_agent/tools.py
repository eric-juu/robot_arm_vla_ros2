"""
The agent's tools, defined once.

Every runtime uses the same set:
- the agent's own loop for API models (runner.py) gets `specs()` and calls `call()`;
- the MCP server that Claude Code and Codex drive (mcp_server.py) registers `functions()`;
- code mode's `k1` object exposes the tools marked `program`.

A tool is a typed method decorated with @tool. Its JSON schema comes from the signature (pydantic), the same way
the MCP SDK builds it, so every model sees the same names, arguments and descriptions, and every call is
validated the same way.

Profiles decide which tools a session gets:
    teach    free joint motion (move_joints, move_joint_delta, save_pose); for authoring, with a human present
    operate  taught poses, the gripper and approved code skills only; for running a bench unattended
k1_web enforces the same split on its side (`agent_free_motion`), so a client can't talk its way around it.
"""
import functools
import hashlib
import inspect
import json
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError, create_model

from k1_agent.primitives import ARM_JOINTS, Primitives, fail
from k1_agent.providers.base import ToolSpec

PROFILES = ("teach", "operate")
TEACH_ONLY = ("teach",)
MOTION_TOOLS = {"move_joints", "move_joint_delta", "go_pose", "go_home", "set_gripper"}

Joint = Literal[tuple(ARM_JOINTS)]
Timeout = Annotated[float, Field(description="seconds to wait for the move to finish", gt=0, le=30)]
Positions = Annotated[
    Annotated[list[float], Field(min_length=len(ARM_JOINTS), max_length=len(ARM_JOINTS))] | dict[str, float],
    Field(description=f"all {len(ARM_JOINTS)} angles in the order {ARM_JOINTS}, or an object with only the "
                      "joints to change")]


@dataclass(frozen=True)
class ToolInfo:
    frame: bool = False         # the result comes with the camera frame taken after the call
    program: bool = False       # also callable as k1.<name>(...) from a code-mode program
    profiles: tuple = PROFILES  # the profiles the tool exists in
    needs: str | None = None    # name of a Toolbox property that must be true (e.g. "has_operator")


def tool(**info):
    def wrap(fn):
        fn.tool_info = ToolInfo(**info)
        return fn
    return wrap


def _strip_titles(schema):
    if isinstance(schema, dict):
        return {k: _strip_titles(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_strip_titles(v) for v in schema]
    return schema


@functools.lru_cache(maxsize=None)
def _arg_model(fn):
    fields = {}
    for p in inspect.signature(fn).parameters.values():
        if p.name == "self":
            continue
        annotation = Any if p.annotation is inspect.Parameter.empty else p.annotation
        fields[p.name] = (annotation, ... if p.default is inspect.Parameter.empty else p.default)
    return create_model(f"{fn.__name__}_arguments", **fields)


def describe(fn) -> str:
    return (inspect.getdoc(fn) or "").strip()


class Toolbox:
    """The tool registry and dispatcher. Subclasses add tools; `done` (finishing the task) is common to all."""

    profile = "teach"

    def __init__(self, trace=None):
        self.trace = trace
        self.outcome: tuple[bool, str] | None = None     # set by the finishing tool

    # ---- registry ----

    @classmethod
    def all_tools(cls) -> list[str]:
        """Every tool this class defines, in definition order (base class tools last)."""
        names = []
        for klass in cls.__mro__:
            for name, member in vars(klass).items():
                if hasattr(member, "tool_info") and name not in names:
                    names.append(name)
        return names

    @classmethod
    def info(cls, name: str) -> ToolInfo:
        return getattr(cls, name).tool_info

    def available(self) -> list[str]:
        """The tools of this session: in its profile, and with what they need (an operator, --allow-enable...)."""
        out = []
        for name in self.all_tools():
            info = self.info(name)
            if self.profile in info.profiles and (info.needs is None or getattr(self, info.needs)):
                out.append(name)
        return out

    def specs(self, only: list[str] | None = None) -> list[ToolSpec]:
        names = [n for n in self.available() if only is None or n in only]
        return [ToolSpec(n, describe(getattr(type(self), n)), self.schema(n)) for n in names]

    @classmethod
    def schema(cls, name: str) -> dict:
        return _strip_titles(_arg_model(getattr(cls, name)).model_json_schema())

    @classmethod
    def schema_digest(cls) -> str:
        """Hash of every tool's name, description and schema, for the eval's version record."""
        text = json.dumps([[n, describe(getattr(cls, n)), cls.schema(n)] for n in cls.all_tools()], sort_keys=True)
        return hashlib.sha1(text.encode()).hexdigest()[:10]

    def functions(self) -> list:
        return [getattr(self, n) for n in self.available()]

    # ---- calling ----

    def call(self, name: str, arguments: dict | None) -> tuple[dict, bytes | None]:
        """Validate and run one tool call. Returns (result, camera frame or None). Never raises for a bad call:
        the model gets a typed error it can react to (StoppedByOperator still propagates)."""
        if name not in self.all_tools():
            return self._refuse(name, fail("unknown_tool", f"no tool {name!r}; tools: {self.available()}"))
        if name not in self.available():
            return self._refuse(name, fail("unavailable", f"{name} isn't available in this session "
                                                          f"(profile {self.profile})"))
        method = getattr(self, name)
        try:
            parsed = _arg_model(getattr(type(self), name)).model_validate(arguments or {})
        except ValidationError as e:
            errors = "; ".join(f"{'.'.join(str(x) for x in err['loc']) or 'arguments'}: {err['msg']}"
                               for err in e.errors()[:5])
            return self._refuse(name, fail("bad_arguments", errors))
        result = method(**{k: getattr(parsed, k) for k in type(parsed).model_fields})
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        return result, self.frame_for(name, result)

    def _refuse(self, name: str, result: dict) -> tuple[dict, None]:
        if self.trace is not None:      # refusals before a primitive runs are traced too (the eval's audit)
            self.trace.log("tool_error", name=name, error=result["error"], message=result["message"])
        return result, None

    def frame_for(self, name: str, result: dict) -> bytes | None:
        return result.pop("image", None) if isinstance(result, dict) else None

    def narrate(self, text: str):
        """Show a line to the people watching (the web page's agent log for the arm)."""

    def begin(self) -> tuple[str, list[dict]]:
        """Start a task: the system prompt, and the parts of the first message that show the starting state."""
        raise NotImplementedError

    # ---- common tools ----

    @tool()
    def done(self, success: bool, summary: str) -> dict:
        """Finish the task. Only claim success if fresh evidence (the latest camera frame) shows it; otherwise
        call done with success false and say what went wrong."""
        self.outcome = (bool(success), str(summary))
        return {"ok": True}


class ArmToolbox(Toolbox):
    """The K1 arm's tools, over the primitive API (which applies the interlocks and writes the trace)."""

    def __init__(self, prims: Primitives, skills=None, operator=None, profile: str = "teach",
                 program_timeout: float = 120.0):
        super().__init__(prims.trace)
        if profile not in PROFILES:
            raise ValueError(f"profile must be one of {PROFILES}, not {profile!r}")
        self.prims = prims
        self.skills = skills
        self.operator = operator
        self.profile = profile
        self.program_timeout = program_timeout

    # ---- session ----

    @property
    def has_operator(self) -> bool:
        return self.operator is not None

    @property
    def has_skills(self) -> bool:
        return self.skills is not None

    @property
    def can_enable(self) -> bool:
        return self.prims.allow_enable

    def program_api(self) -> list[str]:
        """Names a code-mode program may call on `k1` in this session."""
        return [n for n in self.available() if self.info(n).program]

    def frame_for(self, name: str, result: dict) -> bytes | None:
        image = result.pop("image", None) if isinstance(result, dict) else None
        if image is None and self.info(name).frame:
            image = self.prims.last_frame
        return image

    def narrate(self, text: str):
        self.prims.note(text)

    def begin(self) -> tuple[str, list[dict]]:
        from k1_agent.prompts import system_prompt
        from k1_agent.runner import observation_parts

        obs, frame = self.call("observe", {})
        return system_prompt(self, obs), observation_parts(obs, frame, "Current observation:")

    def run_program(self, code: str):
        """Run a code-mode program against this session's program API (sandboxed, see sandbox.py)."""
        from k1_agent.sandbox import run_program

        def dispatch(name, args, kwargs):
            return getattr(self.prims, name)(*args, **kwargs)

        return run_program(code, dispatch, self.program_api(), timeout=self.program_timeout)

    # ---- looking ----

    @tool()
    def get_capability_graph(self) -> dict:
        """What this session can do: the profile, tools, interlocks, step budget, saved poses and k1_web's own
        agent policy."""
        try:
            state = self.prims.client.state
        except Exception:
            state = {}
        return {
            "ok": True,
            "robot": "Unitree K1, 6-DoF arm + gripper, wrist camera (no depth)",
            "profile": self.profile,
            "tools": self.available(),
            "allow_motion": self.prims.allow_motion,
            "allow_enable": self.prims.allow_enable,
            "steps_left": self.prims.max_steps - self.prims.steps_used,
            "max_step_rad": self.prims.max_step_rad,
            "motors_ready": state.get("ready"),
            "poses": state.get("poses", []),
            "server_policy": state.get("agent_policy"),
            "code_skills": [s["name"] for s in self.skills.list() if s.get("code")] if self.skills else [],
        }

    @tool(frame=True, program=True)
    def observe(self) -> dict:
        """Arm state (joint angles in rad, gripper, limits, saved poses) and a fresh wrist-camera image."""
        return self.prims.observe()

    @tool(program=True)
    def list_poses(self) -> dict:
        """Names of the saved (taught) poses."""
        return self.prims.list_poses()

    # ---- moving ----

    @tool(frame=True, program=True, profiles=TEACH_ONLY)
    def move_joints(self, positions: Positions, timeout: Timeout = 10.0) -> dict:
        """Move to absolute joint angles in radians, smoothly, and wait until reached."""
        return self.prims.move_joints(positions, timeout)

    @tool(frame=True, program=True, profiles=TEACH_ONLY)
    def move_joint_delta(self, joint: Joint, delta: float, timeout: Timeout = 10.0) -> dict:
        """Move one joint by a small relative amount in radians (capped per call)."""
        return self.prims.move_joint_delta(joint, delta, timeout)

    @tool(frame=True, program=True)
    def go_pose(self, name: str, timeout: Timeout = 10.0) -> dict:
        """Move to a saved, named (taught) pose."""
        return self.prims.go_pose(name, timeout)

    @tool(frame=True, program=True)
    def go_home(self) -> dict:
        """Move to the 'home' pose."""
        return self.prims.go_home()

    @tool(frame=True, program=True)
    def set_gripper(self, value: Annotated[float, Field(ge=0.0, le=1.0)]) -> dict:
        """Set the gripper: 1.0 fully open, 0.0 closed. Values in between partly close it."""
        return self.prims.set_gripper(value)

    @tool(program=True)
    def stop(self) -> dict:
        """Hold the current position immediately. Always allowed."""
        return self.prims.stop()

    @tool(program=True)
    def wait(self, seconds: Annotated[float, Field(ge=0.0, le=10.0)]) -> dict:
        """Pause for up to 10 seconds."""
        return self.prims.wait(seconds)

    @tool(profiles=TEACH_ONLY)
    def save_pose(self, name: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")]) -> dict:
        """Save the current measured pose under `name`, so it can be replayed with go_pose (teaching)."""
        return self.prims.save_pose(name)

    @tool(needs="can_enable")
    def enable(self, value: bool) -> dict:
        """Lock (true) or release (false) the motors. Releasing makes the arm fall."""
        return self.prims.enable(value)

    # ---- people ----

    @tool(program=True)
    def note(self, message: str) -> dict:
        """Show a line in the web page's agent log, so the operator can follow along."""
        return self.prims.note(message)

    @tool(needs="has_operator")
    def ask_operator(self, question: str) -> dict:
        """Ask the human operator a question (for example to place or reset an object, or when unsure whether
        something is safe). Returns their answer."""
        answer = self.operator.ask(question)
        if self.trace is not None:
            self.trace.log("operator", question=question, answer=answer)
        return {"ok": True, "answer": answer}

    # ---- skills ----

    @tool(needs="has_skills")
    def list_skills(self) -> dict:
        """Names and descriptions of the skills in the library. Skills marked (code) run with run_skill."""
        return {"ok": True, "skills": self.skills.index_text()}

    @tool(needs="has_skills")
    def read_skill(self, name: str) -> dict:
        """Full text of one skill (and its program, for a code skill)."""
        text = self.skills.read(name)
        if text is None:
            return fail("unknown_skill", f"no skill {name!r}; skills: {[s['name'] for s in self.skills.list()]}")
        code = self.skills.read_code(name)
        return {"ok": True, "skill": text, **({"program": code} if code else {})}

    @tool(frame=True, needs="has_skills")
    def run_skill(self, name: str) -> dict:
        """Run an approved code skill (a tested program) on the arm, without improvising the moves. Returns what
        the program returned and printed, and a fresh camera frame."""
        code = self.skills.read_code(name)
        if code is None:
            names = [s["name"] for s in self.skills.list() if s.get("code")]
            return fail("unknown_skill", f"no code skill {name!r}; code skills: {names}")
        result = self.run_program(code)
        from k1_agent.sandbox import jsonable

        observation = self.prims.observe()
        return {"ok": result.ok and result.returned is not False, "skill": name, "error": result.error,
                "returned": jsonable(result.returned), "printed": result.output[-20:],
                "joints": observation.get("joints"), "image": observation.get("image")}

    @tool(needs="has_skills")
    def save_skill_draft(self, name: str, markdown: str, code: str | None = None) -> dict:
        """Propose a new skill: markdown with name/description front matter, and optionally a Python program
        (`def run(k1): ...`) that does it. A human reviews and approves drafts before they are used."""
        try:
            path = self.skills.save_draft(name, markdown)
            if code:
                self.skills.save_code_draft(name, code)
        except ValueError as e:
            return fail("not_saved", str(e))
        return {"ok": True, "saved_for_review": path}
