"""
Runs one agent-written program in a separate process (started by sandbox.py with `python -I`).
Self-contained on purpose: it imports nothing from k1_agent and has no credentials. The program
reaches the arm only through the `k1` proxy, whose calls go over stdin/stdout to the parent.

Protocol, one JSON object per line:
    parent -> child  {"code": str, "api": [names]}, then {"result": ...} or {"stopped": true} per call
    child -> parent  {"call": name, "args": [...], "kwargs": {...}}, {"print": str}, finally {"done": true, ...}
"""
import builtins
import json
import sys
import traceback

ALLOWED_MODULES = {"math"}
SAFE_BUILTINS = [
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter", "float", "format", "frozenset",
    "int", "isinstance", "len", "list", "map", "max", "min", "pow", "print", "range", "repr", "reversed",
    "round", "set", "sorted", "str", "sum", "tuple", "zip",
    "Exception", "ValueError", "RuntimeError", "KeyError", "IndexError", "TypeError", "ZeroDivisionError",
]

proto_in, proto_out = sys.stdin, sys.stdout


def send(obj):
    proto_out.write(json.dumps(obj, default=str) + "\n")
    proto_out.flush()


def recv():
    line = proto_in.readline()
    if not line:
        raise SystemExit(1)
    return json.loads(line)


class Stopped(BaseException):
    """STOP was pressed; not an Exception, so `except Exception` in a program doesn't swallow it."""


class Printer:
    """print() writes in pieces; send whole lines."""

    def __init__(self):
        self.buffer = ""

    def write(self, text):
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            send({"print": line})
        return len(text)

    def flush(self):
        if self.buffer:
            send({"print": self.buffer})
            self.buffer = ""


class K1Proxy:
    def __init__(self, names):
        for name in names:
            setattr(self, name, self._make(name))

    @staticmethod
    def _make(name):
        def call(*args, **kwargs):
            send({"call": name, "args": list(args), "kwargs": kwargs})
            reply = recv()
            if reply.get("stopped"):
                raise Stopped()
            return reply["result"]
        call.__name__ = name
        return call


def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name not in ALLOWED_MODULES or level:
        raise ImportError(f"only these modules can be imported: {sorted(ALLOWED_MODULES)}")
    return builtins.__import__(name, globals, locals, fromlist, level)


def main():
    header = recv()
    printer = sys.stdout = Printer()
    safe ={name: getattr(builtins, name) for name in SAFE_BUILTINS}
    safe["__import__"] = safe_import
    scope = {"__builtins__": safe, "__name__": "k1_program"}
    try:
        exec(compile(header["code"], "program.py", "exec"), scope)
        run = scope.get("run")
        if not callable(run):
            raise RuntimeError("the program must define run(k1)")
        returned = run(K1Proxy(header["api"]))
        printer.flush()
        send({"done": True, "returned": returned})
    except Stopped:
        send({"done": True, "stopped": True})
    except BaseException:
        lines = traceback.format_exc().splitlines()
        # keep the traceback from the program's own frames on
        start = next((i for i, line in enumerate(lines) if 'File "program.py"' in line), 0)
        send({"done": True, "error": "\n".join(lines[:1] + lines[start:])})


if __name__ == "__main__":
    main()
