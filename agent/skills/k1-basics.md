---
name: k1-basics
description: Joint names, units, gripper values and the safety rules of this arm. Read this first.
---

- Joints, in command order: shoulder_pan (turns the whole arm around the base), shoulder_lift, elbow_flex,
  wrist_roll, wrist_flex, gripper_roll. All in radians. 0 rad on every joint is the "zero" pose.
- Gripper: set_gripper(1.0) opens fully, set_gripper(0.0) closes. observe() reports the command
  (gripper_command) and the measured gripper joint (gripper_measured, different units).
- The server clamps every goal to the configured joint limits (observe()["limits"]) and moves at a limited
  speed. If a result has "clamped", the arm stopped at the limit and not at your target.
- "not_reached" usually means the arm is blocked or holding something. Don't push harder: back off, look,
  and rethink.
- Before moving: check observe()["ready"] is true. If not, the motors are off and a human must enable them.
- TODO (measure on the real arm): which sign of shoulder_lift / elbow_flex lifts the gripper, and safe
  heights above the table. Update this skill once known.
