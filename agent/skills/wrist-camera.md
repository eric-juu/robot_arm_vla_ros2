---
name: wrist-camera
description: How to read the wrist camera and calibrate which way the image moves when joints move.
---

- The camera is mounted on the wrist, so the image moves with the gripper. There is no depth: estimate
  distance from apparent size, and from how much things shift when you move a small amount.
- Image directions vs joints are not yet measured for this arm. Calibrate at the start of a run when it
  matters:
  1. observe() and note where a distinctive object is in the image.
  2. move_joint_delta("shoulder_pan", 0.05) and observe() again. Note which way the object moved.
  3. Repeat with wrist_flex. Then move both back.
  Record the result (for example "shoulder_pan + moves objects to the left in the image") in a skill draft
  so later runs can skip this.
- Use the camera to check results: an object between the open gripper fingers fills the lower middle of
  the image (TODO: confirm on the real arm).
