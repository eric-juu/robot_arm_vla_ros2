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
(+ - * / **, round, abs, min, max). The instance for (seed, task, trial) is the same for every model, so the
models in one trial face identical tasks (a paired comparison).
"""
import ast
import copy
import operator
import random
import re

PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
       ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Mod: operator.mod}
FUNCS = {"round": round, "abs": abs, "min": min, "max": max}
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


def sample(spec: dict | None, rng: random.Random) -> dict:
    """Draw one value per param, in order; later params may be expressions over earlier ones."""
    names: dict = {}
    for name, s in (spec or {}).items():
        if isinstance(s, dict) and "choice" in s:
            value = rng.choice(s["choice"])
        elif isinstance(s, dict) and "uniform" in s:
            low, high = s["uniform"]
            value = round(rng.uniform(low, high), s.get("round", 3))
        elif isinstance(s, dict) and "int" in s:
            value = rng.randint(*s["int"])
        elif isinstance(s, str) and s.startswith("="):
            value = evaluate(s[1:], names)
        else:
            value = s
        names[name] = fill(value, names) if isinstance(value, str) else value
    return names


def instance(task: dict, trial: int, seed: int = 0) -> dict:
    """The concrete task for one trial: params sampled with a seed made of (seed, task id, trial)."""
    if not task.get("params"):
        return dict(task, params_used={})
    rng = random.Random(f"{seed}|{task['id']}|{trial}")
    names = sample(task["params"], rng)
    body = {k: v for k, v in copy.deepcopy(task).items() if k not in ("params", "id", "tier")}
    return {"id": task["id"], "tier": task["tier"], **fill(body, names), "params_used": names}
