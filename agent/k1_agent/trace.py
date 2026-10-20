"""
Run traces, in the spirit of ASPIRE's closed-loop execution engine: every primitive call is written
to runs/<run>/trace.jsonl with its arguments, result and the arm state before and after, plus a
camera frame (frame_NNN.jpg) for calls that look or move. The repair loop reads these back to
find where a program went wrong, and they double as a dataset.
"""
import json
import os
import re
import time
from datetime import datetime


class Trace:

    def __init__(self, root: str, name: str = ""):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")[:40]
        self.dir = os.path.join(os.path.expanduser(root), f"{stamp}-{slug}" if slug else stamp)
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "trace.jsonl")
        self.entries: list[dict] = []
        self._frames = 0

    def _write(self, entry: dict):
        self.entries.append(entry)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def record(self, name: str, args: dict, result: dict, before: list | None = None,
               after: list | None = None, frame: bytes | None = None) -> dict:
        """One primitive call."""
        entry = {"t": time.time(), "kind": "call", "name": name, "args": args, "result": result,
                 "joints_before": before, "joints_after": after}
        if frame:
            self._frames += 1
            entry["frame"] = f"frame_{self._frames:03d}.jpg"
            with open(os.path.join(self.dir, entry["frame"]), "wb") as f:
                f.write(frame)
        self._write(entry)
        return entry

    def log(self, kind: str, **data) -> dict:
        """Anything else worth keeping: LLM replies, programs, operator answers, outcomes."""
        entry = {"t": time.time(), "kind": kind, **data}
        self._write(entry)
        return entry

    def save_text(self, filename: str, text: str) -> str:
        path = os.path.join(self.dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def calls_since(self, index: int) -> list[dict]:
        return [e for e in self.entries[index:] if e["kind"] == "call"]

    @staticmethod
    def summarize(calls: list[dict], limit: int = 40) -> str:
        """Compact text view of primitive calls, for the repair prompt."""
        lines = []
        for e in calls[-limit:]:
            result = {k: v for k, v in e["result"].items() if k != "image"}
            lines.append(f"- {e['name']}({json.dumps(e['args'])}) -> {json.dumps(result, default=str)}")
        if len(calls) > limit:
            lines.insert(0, f"(first {len(calls) - limit} calls omitted)")
        return "\n".join(lines) if lines else "(no primitive calls)"
