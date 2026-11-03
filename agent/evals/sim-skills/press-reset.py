def run(k1):
    for name in ("bench_above", "bench_press", "bench_above"):
        result = k1.go_pose(name)
        if not result["ok"]:
            print("go_pose", name, "failed:", result.get("error"), result.get("message"))
            return False
    return True
