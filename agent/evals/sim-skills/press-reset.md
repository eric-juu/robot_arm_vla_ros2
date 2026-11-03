---
name: press-reset
description: Press the dev board's reset button once with the taught poses (simulator bench). Code skill.
---
A taught bench action, as the post-silicon plan has them: the poses `bench_above` (5 cm over the button, fingers
closed and pointing down) and `bench_press` were taught once, and the program replays them. Run it with
`run_skill("press-reset")` instead of improvising the motion; it returns True when every move was reached.
Only for the simulator's "bench" scene (`k1-agent eval`); the real arm has no such poses until they are taught.
