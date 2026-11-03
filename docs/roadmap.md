# Roadmap

Where this is going: the K1 as a "lab hand" for post-silicon validation benches. It does the manual steps of existing test scripts that can't be wired electrically (reset, boot-mode and power buttons, large switches, a touchscreen), and its camera reads what scripts can't (LEDs, 7-segment displays, small screens). The arm is not for plugging cables, socketed chips, probing test points or fine DIP switches.

The key decision: **the LLM is not in the motion loop at test time.** Bench actions are taught once, replayed deterministically, and verified by cheap vision. The LLM triages failures, reads hard displays, writes reports and helps author new actions, with a person approving them. Done so far for that: code skills replayed without a model (`k1-agent skills run`), the operate profile, k1_web's agent policy, and the bench-incident evaluation.

## Next

1. **The bench layer** (`k1_agent/bench.py`, a `station.yaml` per bench):
   - fixtures with ArUco markers: go to the fixture's view pose, find the marker, and refuse to act if the board moved;
   - actions as taught poses: approach, press, retract, then check with the camera;
   - a `press` primitive that stops on contact (tracking error between commanded and measured, since the K1 has no force sensing);
   - LED checks as ROI thresholds (OpenCV, CPU); displays read by a fast model;
   - teach mode: jog from the page, save the poses, dry-run each action 5x and report repeatability;
   - a Python API and pytest fixture for test scripts: `bench.press("evk1.reset")`, `bench.check_led("evk1.pwr", "on")`.
2. **The validation agent** (`k1-agent triage`): DUT tools (UART read/send, run an allow-listed test, power cycle through a relay), a station policy with budgets, and `report(status, diagnosis, repro)`. Measure it first with `k1-agent eval incidents`; extend the emulated board with the real boards' failure modes.
3. **Precision**: forward and inverse kinematics from the URDF for `move_to_pose(xyz)` and taught positions that survive small board moves; an overview camera; later an RGB-D camera and an open-vocabulary detector on a GPU host.
4. **Evaluation**: tasks paired between the simulator and the real arm, so `k1-agent eval correlate` can show whether the simulated tier picks the same models as the arm.
