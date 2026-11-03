"""
What AI agents may do, enforced by k1_web itself. No ROS imports.

The agent also has interlocks of its own (--allow-motion, --max-steps, its profile), but those live in the
client, and anything with the token could skip them. These checks run in the server, for every connection
whose role is "agent": a connection signed in with the agent token (K1_WEB_AGENT_TOKEN) is always an agent,
and a connection with the main token that says hello as an agent gets the same rules.

    allow_motion   agents may send goals, poses and gripper commands at all
    allow_enable   agents may enable or release the motors (off: a person does that)
    free_motion    agents may send free joint goals and save poses ("teach"); off, they may only replay
                   saved poses and use the gripper ("operate"), which is how an unattended bench should run
    limits         tighter joint bounds for agents (a station's keep-out), inside the arm's own limits;
                   free goals are clamped to them, a saved pose outside them is refused
    max_velocity   speed cap for agent goals, below the arm's own max_joint_velocity
"""
from dataclasses import asdict, dataclass

from k1_web.control import N_ARM_JOINTS, CommandError, clamp


@dataclass
class AgentPolicy:
    allow_motion: bool = True
    allow_enable: bool = False
    free_motion: bool = True
    limits_min: list[float] | None = None
    limits_max: list[float] | None = None
    max_velocity: float | None = None

    def __post_init__(self):
        for name in ("limits_min", "limits_max"):
            value = getattr(self, name)
            if value is not None and len(value) == 0:
                setattr(self, name, None)       # [] from a ROS parameter means "the arm's limits"
            elif value is not None and len(value) != N_ARM_JOINTS:
                raise ValueError(f"agent {name} needs {N_ARM_JOINTS} values, got {len(value)}")
        if (self.limits_min is None) != (self.limits_max is None):
            raise ValueError("set both agent_joint_limits_min and agent_joint_limits_max, or neither")
        if self.limits_min is not None and any(lo > hi for lo, hi in zip(self.limits_min, self.limits_max)):
            raise ValueError("every agent joint limit min must be <= its max")
        if self.max_velocity is not None and self.max_velocity <= 0.0:
            self.max_velocity = None            # 0 from a ROS parameter means "the arm's speed"

    def limits(self, arm_min: list[float], arm_max: list[float]) -> tuple[list[float], list[float]]:
        """The bounds agent goals are held to: the agent limits, never wider than the arm's."""
        if self.limits_min is None:
            return list(arm_min), list(arm_max)
        return ([max(a, b) for a, b in zip(self.limits_min, arm_min)],
                [min(a, b) for a, b in zip(self.limits_max, arm_max)])

    def check(self, kind: str):
        """Raise CommandError if an agent may not send this message type."""
        if kind in ("goal", "pose", "gripper") and not self.allow_motion:
            raise CommandError("k1_web refuses motion from agents (agent_allow_motion is false)")
        if kind in ("goal", "save_pose") and not self.free_motion:
            raise CommandError(f"k1_web refuses free joint motion from agents ({kind}); "
                               "agent_free_motion is false, so use a saved pose")
        if kind == "enable" and not self.allow_enable:
            raise CommandError("k1_web refuses enable from agents (agent_allow_enable is false); "
                               "a person enables the motors from the page")

    def clamp_goal(self, positions: list[float], arm_min, arm_max) -> list[float]:
        low, high = self.limits(arm_min, arm_max)
        return clamp(positions, low, high)

    def check_pose(self, joints: list[float], arm_min, arm_max):
        low, high = self.limits(arm_min, arm_max)
        outside = [i for i, (v, a, b) in enumerate(zip(joints, low, high)) if not a - 1e-9 <= v <= b + 1e-9]
        if outside:
            raise CommandError(f"that pose is outside the agent's joint limits (joints {outside}); k1_web refuses it")

    def summary(self) -> dict:
        """What the page and the agent are told about the policy (in every state message)."""
        return asdict(self)
