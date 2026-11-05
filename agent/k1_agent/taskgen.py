"""
Parametric evaluation tasks: a task in tasks.yaml can be a template whose `params` are sampled for each trial, so
ten trials are ten different instances (target, joint, wording, object position...) rather than one prompt
repeated, and tuning skills on a fixed prompt can't inflate the score.

    params:
      joint: {choice: [shoulder_pan, elbow_flex, wrist_flex]}
      delta: {choice: [-0.3, -0.2, 0.2, 0.3]}
      bx: {uniform: [0.27, 0.32], round: 3}
      target: "=delta * 2"                 # computed from the params above
    prompt: "Move {joint} by {delta} rad, then finish."
    check:
      - joints: {"{joint}": "=delta"}

Any string is filled in with `{name}`; a string that starts with "=" is an arithmetic expression over the params
(+ - * / **, round, abs, min, max, len, numbered). The instance for (seed, task, trial) is the same for every
model, so the models in one trial face identical tasks (a paired comparison).

Long tasks need lists. A `count` turns choice / uniform / int into a list, with no value twice in a row;
`waypoints` makes a tour of poses, each changing every listed joint; `numbered()` writes a list out for a prompt:

    params:
      codes: {int: [10, 99], count: "=n"}                     # [37, 12, 85, ...]
      tour: {waypoints: "=n", joints: [shoulder_pan, wrist_flex], values: [-0.4, 0.0, 0.4]}
      tour_text: "=numbered(tour)"                            # "1) shoulder_pan 0.4, wrist_flex -0.4; 2) ..."

`levels: {n: [4, 8, 16, 32]}` expands one task into a task per value (ids `<id>-n4` ...), with `n` set as a
param, so a report can show success against task length (see expand_levels).
"""
import ast
import copy
import operator
import random
import re

PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
       ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Mod: operator.mod}
def numbered(items) -> str:
    """'1) a; 2) b' for a list; a dict item becomes 'joint value, joint value'."""
    def text(item):
        return ", ".join(f"{k} {v}" for k, v in item.items()) if isinstance(item, dict) else str(item)
    return "; ".join(f"{i}) {text(item)}" for i, item in enumerate(items, 1))


FUNCS = {"round": round, "abs": abs, "min": min, "max": max, "len": len, "numbered": numbered}
COMPARE = {ast.Gt: operator.gt, ast.Lt: operator.lt, ast.GtE: operator.ge, ast.LtE: operator.le, ast.Eq: operator.eq,
           ast.NotEq: operator.ne}


class TaskError(ValueError):
    pass


def evaluate(expr: str, names: dict):
    """A small, safe arithmetic evaluator for task templates."""
    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str)):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in names:
                raise TaskError(f"unknown name {node.id!r} in {expr!r}")
            return names[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in OPS:
            return OPS[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
            return OPS[type(node.op)](walk(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCS and not node.keywords:
            return FUNCS[node.func.id](*[walk(a) for a in node.args])
        if isinstance(node, ast.Subscript):
            return walk(node.value)[walk(node.slice)]
        if isinstance(node, ast.IfExp):
            return walk(node.body) if walk(node.test) else walk(node.orelse)
        if isinstance(node, ast.Compare) and all(type(op) in COMPARE for op in node.ops):
            left, ok = walk(node.left), True
            for op, right_node in zip(node.ops, node.comparators):
                right = walk(right_node)
                ok = ok and COMPARE[type(op)](left, right)
                left = right
            return ok
        if isinstance(node, (ast.List, ast.Tuple)):
            return [walk(e) for e in node.elts]
        raise TaskError(f"not allowed in a task expression: {ast.dump(node)[:60]} (in {expr!r})")

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise TaskError(f"bad expression {expr!r}: {e.msg}") from e
    value = walk(tree)
    return round(value, 6) if isinstance(value, float) else value


def fill(value, names: dict):
    """Fill `{name}` placeholders and `=expressions` throughout a task value (dicts, lists, strings)."""
    if isinstance(value, dict):
        return {fill(k, names): fill(v, names) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, names) for v in value]
    if isinstance(value, str):
        if value.startswith("="):
            return evaluate(value[1:], names)
        for _ in range(4):          # placeholders may produce placeholders (e.g. a sampled phrasing)
            filled = PLACEHOLDER.sub(lambda m: str(names[m.group(1)]) if m.group(1) in names else m.group(0), value)
            if filled == value:
                break
            value = filled
        return value
    return value


def _count(value, names: dict) -> int:
    value = evaluate(value[1:], names) if isinstance(value, str) and value.startswith("=") else value
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise TaskError(f"a count must be a whole number of at least 1, got {value!r}")
    return value


def _draw(s: dict, rng: random.Random):
    if "choice" in s:
        return rng.choice(s["choice"])
    if "uniform" in s:
        low, high = s["uniform"]
        return round(rng.uniform(low, high), s.get("round", 3))
    return rng.randint(*s["int"])


def _sequence(s: dict, count: int, rng: random.Random) -> list:
    """`count` draws, no value twice in a row (when there is more than one value to draw from)."""
    out = []
    for _ in range(count):
        value = _draw(s, rng)
        for _ in range(50):
            if not out or value != out[-1]:
                break
            value = _draw(s, rng)
        out.append(value)
    return out


def _waypoints(s: dict, count: int, rng: random.Random) -> list[dict]:
    """A tour of `count` poses over `joints`; every joint changes at every step, and the first pose isn't all 0."""
    joints, values = s.get("joints"), s.get("values")
    if not joints or not values or len(values) < 2:
        raise TaskError("waypoints need joints and at least two values")
    tour, previous = [], {j: 0.0 for j in joints}
    for _ in range(count):
        pose = {j: rng.choice([v for v in values if v != previous[j]]) for j in joints}
        tour.append(pose)
        previous = pose
    return tour


def sample(spec: dict | None, rng: random.Random) -> dict:
    """Draw one value per param, in order; later params may be expressions over earlier ones."""
    names: dict = {}
    for name, s in (spec or {}).items():
        if isinstance(s, dict) and "waypoints" in s:
            value = _waypoints(s, _count(s["waypoints"], names), rng)
        elif isinstance(s, dict) and any(k in s for k in ("choice", "uniform", "int")):
            value = _sequence(s, _count(s["count"], names), rng) if "count" in s else _draw(s, rng)
        elif isinstance(s, str) and s.startswith("="):
            value = evaluate(s[1:], names)
        else:
            value = s
        names[name] = fill(value, names) if isinstance(value, str) else value
    return names


def expand_levels(task: dict) -> list[dict]:
    """A task with `levels: {name: [values]}` becomes one task per value: id `<id>-<name><value>`, the value set
    as the first param (so other params can use it), and `level: {name, value, group}` for the report."""
    if "levels" not in task:
        return [task]
    levels = task["levels"]
    if not isinstance(levels, dict) or len(levels) != 1:
        raise TaskError(f"task {task.get('id')}: levels needs exactly one param, like levels: {{n: [4, 8, 16]}}")
    (name, values), = levels.items()
    if not isinstance(values, list) or not values:
        raise TaskError(f"task {task.get('id')}: levels.{name} needs a list of values")
    if name in (task.get("params") or {}):
        raise TaskError(f"task {task.get('id')}: {name!r} is both a level and a param")
    out = []
    for value in values:
        copy_ = {k: copy.deepcopy(v) for k, v in task.items() if k != "levels"}
        copy_["id"] = f"{task['id']}-{name}{value}"
        copy_["params"] = {name: value, **(task.get("params") or {})}
        copy_["level"] = {"name": name, "value": value, "group": task["id"]}
        out.append(copy_)
    return out


def instance(task: dict, trial: int, seed: int = 0) -> dict:
    """The concrete task for one trial: params sampled with a seed made of (seed, task id, trial)."""
    if not task.get("params"):
        return dict(task, params_used={})
    rng = random.Random(f"{seed}|{task['id']}|{trial}")
    names = sample(task["params"], rng)
    body = {k: v for k, v in copy.deepcopy(task).items() if k not in ("params", "id", "tier")}
    return {"id": task["id"], "tier": task["tier"], **fill(body, names), "params_used": names}
