"""
`k1-agent eval report`: tables and a chart from results.jsonl rows.

The headline stays success rate (decided by checkers) against cost, ASPIRE-style. Around it, the numbers an
unattended bench needs:

- 95% intervals two ways: Wilson over trials, and a bootstrap over tasks. Task difficulty varies far more than
  trial noise, so the task bootstrap is the honest one when tasks differ.
- pass^k: the chance a model succeeds k times in a row on the same task (estimated without bias from n >= k
  trials per task). 90% pass@1 can be 43% pass^8, and a bench that runs overnight needs the streak.
- claim precision: of the trials where the model said it succeeded, how many really did. In deployment there
  is no checker, only the agent's word.
- Delta vs best: the paired difference to the best model on the same task instances, with a task bootstrap.
- success@1 for code mode, which otherwise gets several attempts against tools mode's one.
- safety: repeated failed moves, refusals, contacts, per trial.
- long tasks (a task with `levels`): success and progress against task length, the length at which success
  falls to 50% (the "horizon", as METR measures it for software tasks), and how runs ended: finished, claimed
  success wrongly, gave up, or ran out of turns, steps or time.
"""
import html as html_lib
import json
import math
import os
import random
from collections import Counter
from datetime import datetime

BOOTSTRAP_SAMPLES = 2000
OUT_OF_BUDGET = {"max_turns", "timeout", "no_done"}      # run endings that mean the agent didn't finish in time
ENDINGS = ["finished", "false success", "gave up", "out of budget", "stopped", "error"]


# ---------------- statistics ----------------

def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def task_bootstrap(per_task: dict, samples: int = BOOTSTRAP_SAMPLES, seed: int = 0) -> tuple[float, float]:
    """95% interval of the pooled success rate, resampling tasks (not trials). per_task: {task: (wins, n)}."""
    tasks = list(per_task)
    if len(tasks) < 2:
        return wilson(*per_task[tasks[0]]) if tasks else (0.0, 0.0)
    rng = random.Random(seed)
    rates = []
    for _ in range(samples):
        picked = [per_task[rng.choice(tasks)] for _ in tasks]
        n = sum(p[1] for p in picked)
        rates.append(sum(p[0] for p in picked) / n if n else 0.0)
    rates.sort()
    return rates[int(0.025 * samples)], rates[int(0.975 * samples) - 1]


def paired_delta(a: dict, b: dict, samples: int = BOOTSTRAP_SAMPLES, seed: int = 0) -> tuple[float, float, float]:
    """Difference in success rate a - b on the tasks both ran, with a 95% task-bootstrap interval."""
    tasks = sorted(set(a) & set(b))
    if not tasks:
        return float("nan"), float("nan"), float("nan")

    def rate(sel, per):
        n = sum(per[t][1] for t in sel)
        return sum(per[t][0] for t in sel) / n if n else 0.0

    point = rate(tasks, a) - rate(tasks, b)
    rng = random.Random(seed)
    deltas = sorted(rate(pick, a) - rate(pick, b) for pick in
                    ([rng.choice(tasks) for _ in tasks] for _ in range(samples)))
    return point, deltas[int(0.025 * samples)], deltas[int(0.975 * samples) - 1]


def pass_hat_k(per_task: dict, k: int) -> float | None:
    """Mean over tasks of C(wins, k) / C(n, k): the chance k fresh trials of a task all succeed."""
    usable = [(c, n) for c, n in per_task.values() if n >= k]
    if k < 1 or not usable:
        return None
    return sum(math.comb(c, k) / math.comb(n, k) for c, n in usable) / len(usable)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation (average ranks for ties)."""
    if len(xs) < 3:
        return None

    def ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            for k in range(i, j + 1):
                out[order[k]] = (i + j) / 2
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return cov / (sx * sy) if sx and sy else None


# ---------------- rows ----------------

def load_rows(paths: list[str]) -> list[dict]:
    rows = []
    for path in paths:
        files = [os.path.join(path, "results.jsonl")] if os.path.isdir(path) else [path]
        for file in files:
            with open(file, encoding="utf-8") as f:
                rows += [json.loads(line) for line in f if line.strip()]
    return rows


def _per_task(group: list[dict], key: str = "suite_task") -> dict:
    out: dict = {}
    for r in group:
        wins, n = out.get(r[key], (0, 0))
        out[r[key]] = (wins + bool(r["success"]), n + 1)
    return out


def summarize_trials(rows: list[dict]) -> list[dict]:
    groups: dict = {}
    for row in rows:
        if row.get("kind") == "trial":
            key = (row["tier"], row["model"], row["mode"], row.get("skills", "on"), row.get("split", "dev"))
            groups.setdefault(key, []).append(row)
    out = []
    for (tier, model, mode, skills, split), group in sorted(groups.items()):
        n, wins = len(group), sum(bool(r["success"]) for r in group)
        costs = [r.get("cost_usd") for r in group]
        known = None not in costs
        per_task = _per_task(group)
        k = min(n_t for _, n_t in per_task.values())
        claims = [r for r in group if r.get("claimed")]
        audits = [r.get("audit") or {} for r in group]
        low, high = wilson(wins, n)
        boot_low, boot_high = task_bootstrap(per_task)
        code = [r for r in group if r["mode"] == "code" and r.get("attempts")]
        out.append({
            "tier": tier, "model": model, "mode": mode, "skills": skills, "split": split, "n": n, "successes": wins,
            "success_rate": wins / n, "ci_low": low, "ci_high": high, "boot_low": boot_low, "boot_high": boot_high,
            "k": k, "pass_k": pass_hat_k(per_task, k) if k >= 2 else None,
            "cost_per_trial": sum(costs) / n if known else None,
            "cost_per_success": (sum(costs) / wins if known and wins else None),
            "false_successes": sum(1 for r in group if r.get("claimed") and not r["success"]),
            "claim_precision": (sum(1 for r in claims if r["success"]) / len(claims)) if claims else None,
            "success_at_1": (sum(1 for r in code if r["success"] and r["attempts"] == 1) / len(code)) if code else None,
            "errors": sum(1 for r in group if r.get("error")),
            "wall_s": sum(r["wall_s"] for r in group) / n,
            "repeated_failures": sum(a.get("repeated_failures", 0) for a in audits) / n,
            "refusals": sum(a.get("refused", 0) for a in audits) / n,
            "contacts": sum(1 for r in group if (r.get("sim") or {}).get("contacts")) / n,
            "tasks": per_task,
        })
    for tier, split in {(g["tier"], g["split"]) for g in out}:
        same = [g for g in out if g["tier"] == tier and g["split"] == split]
        for g in same:
            g["frontier"] = g["cost_per_trial"] is not None and not any(
                o is not g and o["cost_per_trial"] is not None and o["success_rate"] >= g["success_rate"]
                and o["cost_per_trial"] <= g["cost_per_trial"]
                and (o["success_rate"] > g["success_rate"] or o["cost_per_trial"] < g["cost_per_trial"])
                for o in same)
        best = max(same, key=lambda g: g["success_rate"])
        for g in same:
            g["delta_vs_best"] = None if g is best else paired_delta(g["tasks"], best["tasks"])
    return out


def summarize_perception(rows: list[dict]) -> list[dict]:
    groups: dict = {}
    for row in rows:
        if row.get("kind") == "perception":
            groups.setdefault(row["model"], []).append(row)
    out = []
    for model, group in sorted(groups.items()):
        n = len(group)
        costs = [r.get("cost_usd") for r in group]
        categories = sorted({r["category"] for r in group})
        out.append({
            "model": model, "n": n, "accuracy": sum(r["correct"] for r in group) / n,
            "abstained": sum(r["abstained"] for r in group) / n,
            "confident_wrong": sum(1 for r in group if not r["correct"] and not r["abstained"]) / n,
            "cost_per_100": (sum(costs) / n * 100) if None not in costs else None,
            "latency_s": sum(r["latency_s"] for r in group) / n,
            "by_category": {c: sum(r["correct"] for r in group if r["category"] == c) /
                            sum(1 for r in group if r["category"] == c) for c in categories},
        })
    return out


def correlate(rows_a: list[dict], rows_b: list[dict]) -> dict:
    """Do two runs rank the models the same way? (e.g. the simulator against the real arm). Uses the tasks the
    runs share through their `pair` labels when both have them, otherwise each model's overall success."""
    def rates(rows):
        per: dict = {}
        for r in rows:
            if r.get("kind") == "trial":
                per.setdefault(r["model"], []).append(r)
        return per

    a, b = rates(rows_a), rates(rows_b)
    pairs_a = {r.get("pair") for r in rows_a if r.get("pair")}
    pairs_b = {r.get("pair") for r in rows_b if r.get("pair")}
    shared = pairs_a & pairs_b
    models = sorted(set(a) & set(b))
    table = []
    for m in models:
        ra = [r for r in a[m] if not shared or r.get("pair") in shared]
        rb = [r for r in b[m] if not shared or r.get("pair") in shared]
        if ra and rb:
            table.append((m, sum(r["success"] for r in ra) / len(ra), len(ra),
                          sum(r["success"] for r in rb) / len(rb), len(rb)))
    rho = spearman([t[1] for t in table], [t[3] for t in table])
    return {"models": table, "rho": rho, "pairs": sorted(shared)}


# ---------------- markdown ----------------

def _money(value) -> str:
    return "?" if value is None else (f"${value:.4f}" if value < 1 else f"${value:.2f}")


def _pct(value) -> str:
    return "-" if value is None else f"{value:.0%}"


def report_markdown(trials: list[dict], perception: list[dict], extra: list[str] | None = None) -> str:
    lines = []
    for tier, split in sorted({(g["tier"], g["split"]) for g in trials}):
        same = [g for g in trials if g["tier"] == tier and g["split"] == split]
        k = min(g["k"] for g in same)
        lines += [f"## Tier {tier} ({split} tasks): success rate vs cost", "",
                  f"| model | mode | skills | success | 95% CI (tasks) | pass^{k} | $ / trial | $ / success | "
                  "claims true | false successes | Δ vs best | n |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for g in sorted(same, key=lambda g: -g["success_rate"]):
            star = " *" if g.get("frontier") else ""
            delta = g.get("delta_vs_best")
            delta_text = "best" if delta is None else (
                "-" if math.isnan(delta[0]) else f"{delta[0]:+.0%} [{delta[1]:+.0%}, {delta[2]:+.0%}]")
            lines.append(f"| {g['model']}{star} | {g['mode']} | {g['skills']} | {g['success_rate']:.0%} | "
                         f"{g['boot_low']:.0%}-{g['boot_high']:.0%} | {_pct(g['pass_k'])} | "
                         f"{_money(g['cost_per_trial'])} | {_money(g['cost_per_success'])} | "
                         f"{_pct(g['claim_precision'])} | {g['false_successes']} | {delta_text} | {g['n']} |")
        lines += ["", "* = on the frontier: no other model is both at least as successful and at least as cheap. "
                  f"pass^{k}: chance of {k} successes in a row on one task. Claims true: of the trials where the model "
                  "said it succeeded, the share the checker confirmed. Δ vs best: paired difference on the same "
                  "task instances, 95% task-bootstrap interval.", ""]
        lines += ["| model | mode | success@1 (code) | repeated failed moves / trial | refusals / trial | "
                  "trials with contact | errors | avg time |", "|---|---|---|---|---|---|---|---|"]
        for g in same:
            lines.append(f"| {g['model']} | {g['mode']} | {_pct(g['success_at_1'])} | {g['repeated_failures']:.2f} | "
                         f"{g['refusals']:.2f} | {g['contacts']:.0%} | {g['errors']} | {g['wall_s']:.0f} s |")
        lines.append("")
        task_ids = sorted({t for g in same for t in g["tasks"]})
        lines += ["| model | mode | " + " | ".join(task_ids) + " |", "|---|---|" + "---|" * len(task_ids)]
        for g in same:
            cells = [f"{g['tasks'][t][0]}/{g['tasks'][t][1]}" if t in g["tasks"] else "-" for t in task_ids]
            lines.append(f"| {g['model']} | {g['mode']} | " + " | ".join(cells) + " |")
        lines.append("")
    if perception:
        categories = sorted({c for p in perception for c in p["by_category"]})
        lines += ["## Tier 0: perception", "",
                  "| model | accuracy | can't tell | confidently wrong | $ / 100 questions | latency | " +
                  " | ".join(categories) + " |", "|---|---|---|---|---|---|" + "---|" * len(categories)]
        for p in sorted(perception, key=lambda p: -p["accuracy"]):
            cells = [f"{p['by_category'][c]:.0%}" if c in p["by_category"] else "-" for c in categories]
            lines.append(f"| {p['model']} | {p['accuracy']:.0%} | {p['abstained']:.0%} | {p['confident_wrong']:.0%} | "
                         f"{_money(p['cost_per_100'])} | {p['latency_s']:.1f} s | " + " | ".join(cells) + " |")
        lines.append("")
    for section in extra or []:
        lines += [section, ""]
    return "\n".join(lines) or "No results yet."


def report_sections(rows: list[dict]) -> list[str]:
    """The sections after the main tables: long tasks, decision probes, bench incidents (those with rows)."""
    from k1_agent import incidents, probes

    return [s for s in (horizon_markdown(rows), diagnosis_markdown(rows), probes.report(rows), incidents.report(rows))
            if s]


def report_all(rows: list[dict]) -> tuple[str, list[dict], list[dict]]:
    """Markdown for every kind of row in a results file: trials, perception, long tasks, probes, incidents."""
    trials, perception = summarize_trials(rows), summarize_perception(rows)
    return report_markdown(trials, perception, report_sections(rows)), trials, perception


# ---------------- diagnosis tasks ----------------

DIAGNOSIS_ENDINGS = ("finished", "fault reported", "other cause", "unverified", "false fault", "unsupported fault",
                     "false success", "timed out")


def diagnosis_markdown(rows: list[dict]) -> str:
    """How diagnosis runs ended, per model: the success rate alone hides a board blamed with nothing behind it."""
    counts: dict = {}
    for row in rows:
        if row.get("diagnosis_ending"):
            counts.setdefault((row["model"], row["mode"]), Counter())[row["diagnosis_ending"]] += 1
    if not counts:
        return ""
    lines = ["## Diagnosis tasks: how the runs ended", "",
             "| model | mode | " + " | ".join(DIAGNOSIS_ENDINGS) + " | n |",
             "|---|---|" + "---|" * (len(DIAGNOSIS_ENDINGS) + 1)]
    for (model, mode), c in sorted(counts.items()):
        lines.append(f"| {model} | {mode} | " + " | ".join(str(c[e]) for e in DIAGNOSIS_ENDINGS) +
                     f" | {sum(c.values())} |")
    lines += ["", "False fault: blamed a working board when no press ever registered. Unsupported fault: the same, on a "
              "board that really was hung, so right by luck. Unverified: stopped and said it couldn't tell. "
              "False success: claimed the job done and the checks disagree. Timed out: cut off before it gave an answer, so "
              "it says nothing about the diagnosis. Endings are read from the `cause:` line "
              "with patterns, so spot-check them."]
    return "\n".join(lines)


# ---------------- long tasks ----------------

def ending(row: dict) -> str:
    """How a trial ended: finished (the checker agrees), false success, gave up (said it couldn't), out of budget
    (turns, motion steps or time ran out, or the session ended without an answer), stopped, or error."""
    if row.get("error"):
        return "error"
    if row["success"]:
        return "finished"
    if row.get("claimed"):
        return "false success"
    if row.get("reason") in OUT_OF_BUDGET or (row.get("audit") or {}).get("out_of_steps"):
        return "out of budget"
    return "stopped" if row.get("reason") == "stopped" else "gave up"


def _non_increasing(rates: list[float], weights: list[int]) -> list[float]:
    """The closest non-increasing sequence (pool adjacent violators): longer shouldn't be easier."""
    blocks: list[list[float]] = []          # [weighted sum, weight, count]
    for r, w in zip(rates, weights):
        blocks.append([r * w, w, 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] < blocks[-1][0] / blocks[-1][1]:
            s, w2, c = blocks.pop()
            blocks[-1] = [blocks[-1][0] + s, blocks[-1][1] + w2, blocks[-1][2] + c]
    return [s / w for s, w, c in blocks for _ in range(int(c))]


def horizon_estimate(levels: list[tuple]) -> float:
    """
    The task length at which success falls to 50%. levels: (length, successes, trials) per length. The rates
    are made non-increasing, then interpolated (on a log scale when every length is positive) between the last
    length at or above 50% and the first below. inf: at or above 50% at every length; 0.0: below 50% already at
    the shortest.
    """
    levels = sorted(levels)
    rates = _non_increasing([w / n for _, w, n in levels], [n for _, _, n in levels])
    if rates[0] < 0.5:
        return 0.0
    scale = (math.log2, lambda v: 2 ** v) if all(v > 0 for v, _, _ in levels) else (float, float)
    for i in range(1, len(levels)):
        if rates[i] < 0.5:
            x0, x1 = scale[0](levels[i - 1][0]), scale[0](levels[i][0])
            return scale[1](x0 + (rates[i - 1] - 0.5) / (rates[i - 1] - rates[i]) * (x1 - x0))
    return math.inf


def horizon_interval(levels: list[tuple], samples: int = BOOTSTRAP_SAMPLES, seed: int = 0) -> tuple[float, float]:
    """95% interval of horizon_estimate, resampling the trials at each length."""
    rng = random.Random(seed)
    estimates = sorted(horizon_estimate([(v, sum(rng.random() < w / n for _ in range(n)), n) for v, w, n in levels])
                       for _ in range(samples))
    return estimates[int(0.025 * samples)], estimates[int(0.975 * samples) - 1]


def summarize_horizons(rows: list[dict]) -> list[dict]:
    """Per long task (a `levels` group) and model: success, progress, cost and turns at each length, the horizon,
    and how the runs ended."""
    groups: dict = {}
    for r in rows:
        if r.get("kind") == "trial" and r.get("level"):
            key = (r["level"]["group"], r["level"]["name"])
            groups.setdefault(key, {}).setdefault((r["model"], r["mode"]), []).append(r)
    out = []
    for (group, name), by_model in sorted(groups.items()):
        values = sorted({r["level"]["value"] for rs in by_model.values() for r in rs})
        models = []
        for (model, mode), rs in sorted(by_model.items()):
            per = {}
            for value in values:
                at = [r for r in rs if r["level"]["value"] == value]
                if not at:
                    continue
                wins = sum(bool(r["success"]) for r in at)
                done = [r["progress"] for r in at if r.get("progress") is not None]
                costs = [r.get("cost_usd") for r in at]
                per[value] = {"successes": wins, "n": len(at), "progress": sum(done) / len(done) if done else None,
                              "cost_per_success": sum(costs) / wins if wins and None not in costs else None,
                              "turns": sum(r.get("llm_calls") or 0 for r in at) / len(at)}
            counts = [(v, p["successes"], p["n"]) for v, p in per.items()]
            many = len(counts) >= 2
            models.append({"model": model, "mode": mode, "levels": per,
                           "horizon": horizon_estimate(counts) if many else None,
                           "horizon_ci": horizon_interval(counts) if many else None,
                           "endings": Counter(ending(r) for r in rs)})
        out.append({"group": group, "name": name, "values": values, "models": models})
    return out


def _length(value: float, values: list) -> str:
    if value == math.inf:
        return f"> {max(values)}"
    if value == 0.0:
        return f"< {min(values)}"
    return f"{value:.0f}" if value >= 10 else f"{value:.1f}"


def horizon_markdown(rows: list[dict]) -> str:
    groups = summarize_horizons(rows)
    if not groups:
        return ""
    lines = []
    for g in groups:
        name, values = g["name"], g["values"]
        heads = " | ".join(f"{name}={v}" for v in values)
        lines += [f"## Long task {g['group']}: success · progress by {name}", "",
                  f"| model | mode | {heads} | 50% horizon [95% CI] |", "|---|---|" + "---|" * len(values) + "---|"]
        for m in g["models"]:
            cells = []
            for v in values:
                p = m["levels"].get(v)
                cells.append("-" if p is None else f"{p['successes']}/{p['n']} · {_pct(p['progress'])}")
            h = "-" if m["horizon"] is None else (
                f"{_length(m['horizon'], values)} [{_length(m['horizon_ci'][0], values)}, "
                f"{_length(m['horizon_ci'][1], values)}]")
            lines.append(f"| {m['model']} | {m['mode']} | " + " | ".join(cells) + f" | {h} |")
        lines += ["", f"| model | mode | {heads} |", "|---|---|" + "---|" * len(values)]
        for m in g["models"]:
            cells = []
            for v in values:
                p = m["levels"].get(v)
                cells.append("-" if p is None else f"{_money(p['cost_per_success'])} · {p['turns']:.0f} turns")
            lines.append(f"| {m['model']} | {m['mode']} | " + " | ".join(cells) + " |")
        lines.append("")
    lines += [f"Cells: successes/trials · progress (the share of the task's steps done, from its checker), then "
              "$ per success · model turns per trial. 50% horizon: the length at which success falls to 50%, "
              "interpolated on a log scale; '>' means it never fell that low in this run.", "",
              "## Long tasks: how the runs ended", "",
              "| model | mode | " + " | ".join(ENDINGS) + " | n |", "|---|---|" + "---|" * (len(ENDINGS) + 1)]
    pooled: dict = {}
    for g in groups:
        for m in g["models"]:
            pooled.setdefault((m["model"], m["mode"]), Counter()).update(m["endings"])
    for (model, mode), counts in sorted(pooled.items()):
        total = sum(counts.values())
        lines.append(f"| {model} | {mode} | " + " | ".join(f"{counts[e] / total:.0%}" for e in ENDINGS) +
                     f" | {total} |")
    lines += ["", "Out of budget: the run used up its turns, motion steps or time (or ended without an answer) before "
              "finishing. A long-task budget is about twice what a careful run needs, so this is a failure to "
              "finish, not a tight limit."]
    return "\n".join(lines)


def correlation_markdown(result: dict, label_a: str, label_b: str) -> str:
    lines = [f"## Do {label_a} and {label_b} rank the models the same way?", "",
             f"| model | {label_a} | n | {label_b} | n |", "|---|---|---|---|---|"]
    for model, ra, na, rb, nb in result["models"]:
        lines.append(f"| {model} | {ra:.0%} | {na} | {rb:.0%} | {nb} |")
    rho = result["rho"]
    lines += ["", f"Spearman rank correlation: {'n/a (needs 3+ models in both runs)' if rho is None else f'{rho:.2f}'}"
              + (f", over the paired tasks {result['pairs']}" if result["pairs"] else ", over all tasks") + ".",
              "Near 1: the cheap tier picks the same models as the expensive one, so it is a valid screen. "
              "Low or negative: it is screening for something else."]
    return "\n".join(lines)


# ---------------- html ----------------

def _svg_scatter(groups: list[dict]) -> str:
    """Success rate (y, with 95% task-bootstrap bars) against $ per trial (x, log scale)."""
    points = [g for g in groups if g["cost_per_trial"]]
    if not points:
        return "<p class='muted'>No priced trials to plot yet.</p>"
    width, height, left, right, top, bottom = 720, 420, 64, 24, 20, 56
    lo = math.log10(min(g["cost_per_trial"] for g in points)) - 0.2
    hi = math.log10(max(g["cost_per_trial"] for g in points)) + 0.2
    if hi - lo < 0.5:
        lo, hi = lo - 0.25, hi + 0.25

    def x(cost):
        return left + (math.log10(cost) - lo) / (hi - lo) * (width - left - right)

    def y(rate):
        return top + (1 - rate) * (height - top - bottom)

    parts = [f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='success rate against cost per trial'>"]
    for rate in (0, 0.25, 0.5, 0.75, 1.0):
        parts.append(f"<line class='grid' x1='{left}' x2='{width - right}' y1='{y(rate):.1f}' y2='{y(rate):.1f}'/>"
                     f"<text class='tick' x='{left - 8}' y='{y(rate) + 4:.1f}' text-anchor='end'>{rate:.0%}</text>")
    for exponent in range(math.floor(lo), math.ceil(hi) + 1):
        for mult in (1, 2, 5):
            cost = mult * 10 ** exponent
            if lo <= math.log10(cost) <= hi:
                parts.append(f"<line class='grid' x1='{x(cost):.1f}' x2='{x(cost):.1f}' y1='{top}' "
                             f"y2='{height - bottom}'/><text class='tick' x='{x(cost):.1f}' y='{height - bottom + 18}' "
                             f"text-anchor='middle'>{_money(cost)}</text>")
    parts.append(f"<text class='axis' x='{(left + width - right) / 2}' y='{height - 12}' text-anchor='middle'>"
                 "average cost per trial (log scale)</text>"
                 f"<text class='axis' transform='rotate(-90)' x='{-(top + height - bottom) / 2}' y='16' "
                 "text-anchor='middle'>success rate (checker)</text>")
    for g in points:
        cx, cy = x(g["cost_per_trial"]), y(g["success_rate"])
        label = g["model"].split(":", 1)[-1] + ("" if g["mode"] == "tools" else f" ({g['mode']})") + \
            ("" if g["skills"] == "on" else " no-skills")
        parts.append(f"<line class='ci' x1='{cx:.1f}' x2='{cx:.1f}' y1='{y(g['boot_low']):.1f}' y2='{y(g['boot_high']):.1f}'/>"
                     f"<circle class='{'dot frontier' if g.get('frontier') else 'dot'}' cx='{cx:.1f}' cy='{cy:.1f}' r='6'>"
                     f"<title>{label}: {g['success_rate']:.0%} at {_money(g['cost_per_trial'])}/trial, n={g['n']}</title>"
                     f"</circle><text class='label' x='{cx + 9:.1f}' y='{cy - 8:.1f}'>{label}</text>")
    parts.append("</svg>")
    return "".join(parts)


def report_html(trials: list[dict], perception: list[dict], title: str = "K1 model evaluation",
                extra: list[str] | None = None) -> str:
    def md_table_to_html(md: str) -> str:
        out, in_table = [], False
        for line in md.splitlines():
            if line.startswith("|"):
                cells = [html_lib.escape(c.strip()) for c in line.strip("|").split("|")]
                if set(line) <= set("|-"):
                    continue
                if not in_table:
                    out.append("<div class='scroll'><table><thead><tr>" + "".join(f"<th>{c}</th>" for c in cells) +
                               "</tr></thead><tbody>")
                    in_table = True
                else:
                    out.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
            else:
                if in_table:
                    out.append("</tbody></table></div>")
                    in_table = False
                if line.startswith("## "):
                    out.append(f"<h2>{html_lib.escape(line[3:])}</h2>")
                elif line.strip():
                    out.append(f"<p class='muted'>{html_lib.escape(line)}</p>")
        if in_table:
            out.append("</tbody></table></div>")
        return "\n".join(out)

    body = []
    for tier, split in sorted({(g["tier"], g["split"]) for g in trials}):
        same = [g for g in trials if g["tier"] == tier and g["split"] == split]
        body.append(md_table_to_html(report_markdown(same, [])))
        body.append(f"<figure>{_svg_scatter(same)}</figure>")
    if perception or extra:
        body.append(md_table_to_html(report_markdown([], perception, extra)))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_lib.escape(title)}</title>
<style>
:root {{ --bg: #fbfbfa; --fg: #1f2328; --muted: #6a6f76; --line: #e3e4e6; --accent: #2f6fdb; --star: #d97706; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg: #16181c; --fg: #e8e9eb;
  --muted: #9aa0a8; --line: #2c2f35; --accent: #6ea0ff; --star: #f5a524; }} }}
:root[data-theme="dark"] {{ --bg: #16181c; --fg: #e8e9eb; --muted: #9aa0a8; --line: #2c2f35; --accent: #6ea0ff;
  --star: #f5a524; }}
body {{ background: var(--bg); color: var(--fg); font: 15px/1.5 system-ui, sans-serif; margin: 0 auto;
  max-width: 1100px; padding: 24px 16px 48px; }}
h1 {{ font-size: 1.5rem; margin: 0 0 4px; }} h2 {{ font-size: 1.15rem; margin: 32px 0 8px; }}
.muted {{ color: var(--muted); font-size: 0.9rem; }}
.scroll {{ overflow-x: auto; }} table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
th, td {{ border-bottom: 1px solid var(--line); padding: 6px 10px; text-align: left; white-space: nowrap; }}
th {{ font-weight: 600; }}
figure {{ margin: 16px 0; }} svg {{ width: 100%; height: auto; }}
.grid {{ stroke: var(--line); }} .tick, .label {{ fill: var(--muted); font-size: 11px; }}
.axis {{ fill: var(--fg); font-size: 12px; }} .ci {{ stroke: var(--muted); stroke-width: 1.5; }}
.dot {{ fill: var(--accent); }} .dot.frontier {{ fill: var(--star); }}
</style></head><body>
<h1>{html_lib.escape(title)}</h1>
<p class="muted">Success is decided by each task's checker, not by the model's own claim. Costs are from token
counts and pricing.yaml (Claude Code: its own API-equivalent estimate). Generated {datetime.now():%Y-%m-%d %H:%M}.</p>
{''.join(body)}
</body></html>
"""
