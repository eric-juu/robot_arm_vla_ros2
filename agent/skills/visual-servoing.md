---
name: visual-servoing
description: Step-by-step approach to an object using only the wrist camera (look, small move, look again).
---

1. Center the target: use small shoulder_pan / wrist_flex steps (0.03 to 0.1 rad) until the object is
   in the middle of the image. Re-observe after every step and reduce the step size as you get closer.
2. Approach: change shoulder_lift / elbow_flex in small steps so the object grows in the image while
   staying centered. Correct the centering between steps.
3. Stop approaching when the object fills the space between the gripper fingers. If a move returns
   "not_reached", you may be touching the table: back off by the last step.
4. Grasp: set_gripper(0.0) for small objects, or a partial value for fragile ones, then lift with a
   small shoulder_lift/elbow_flex move and observe to confirm the object came along.
5. If you lose the object from the view, retrace your last steps instead of searching widely.
