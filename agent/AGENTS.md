# Operating rules for an AI agent driving the K1 arm

These rules are for a coding agent (Claude Code, Codex, ...) using the `k1` MCP tools (`k1-agent mcp`). The arm is real, and a person may be next to it.

1. **Start by looking.** Call `get_capability_graph`, then `observe`. Report what you see and whether `motors_ready` is true. If it isn't, ask the operator to enable the motors from the web page. Don't try to enable them yourself unless the `enable` tool exists and the operator asked you to.
2. **Read the skills.** Call `list_skills`, then `read_skill` for each one that applies, starting with `k1-basics`.
3. **Small steps, fresh evidence.**
   - Near objects or the table, move with `move_joint_delta` in steps of 0.15 rad or less.
   - Look at the frame each motion tool returns before the next move.
   - Never chain several blind moves.
4. **Typed failures are information.** On `not_reached`, `clamped`, `rejected` or `safety_interlock`, stop and think. Don't retry the same move harder.
5. **Stop on doubt.** If the frame shows something unexpected (a hand, a collision, a fallen object), call `stop` and ask the operator.
6. **STOP from the web page ends the task.** Every later motion refuses. Tell the operator and wait for a new instruction.
7. **Narrate.** Call `note` with one short line per step ("centering the cup", "closing the gripper"). The operator watches these in the web page's agent log, next to the video.
8. **Verify success from the camera.** Only report a task done when a fresh `observe` frame shows it.
9. **Save what you learned.** After a verified success, or after measuring something about the arm (for example which way the image moves when `shoulder_pan` increases), call `save_skill_draft`. The operator approves drafts with `k1-agent skills approve <name>`.
