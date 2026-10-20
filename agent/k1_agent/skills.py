"""
Skill library (ASPIRE-style): markdown notes on what works on this arm, retrieved into the agent's
context. Each skill is a file with YAML front matter:

    ---
    name: grasp-from-above
    description: one line saying when to use it
    ---
    body: what worked, joint ranges, pitfalls

The agent sees every skill's name and description and reads the full text with read_skill(name).
New skills the agent drafts land in pending/ and only join the library once a human approves them.
"""
from __future__ import annotations     # SkillLibrary.list would shadow the builtin in annotations

import os
import re
import shutil

import yaml

SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


def parse(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            meta = yaml.safe_load(parts[1]) or {}
            return (meta if isinstance(meta, dict) else {}), parts[2].strip()
    return {}, text.strip()


class SkillLibrary:

    def __init__(self, root: str):
        self.root = os.path.expanduser(root)
        self.pending_dir = os.path.join(self.root, "pending")

    def _list(self, directory: str) -> list[dict]:
        if not os.path.isdir(directory):
            return []
        skills = []
        for filename in sorted(os.listdir(directory)):
            if filename.endswith(".md"):
                with open(os.path.join(directory, filename), encoding="utf-8") as f:
                    meta, _ = parse(f.read())
                skills.append({"name": filename[:-3], "description": str(meta.get("description", ""))})
        return skills

    def list(self) -> list[dict]:
        return self._list(self.root)

    def pending(self) -> list[dict]:
        return self._list(self.pending_dir)

    def index_text(self) -> str:
        skills = self.list()
        if not skills:
            return "(no skills yet)"
        return "\n".join(f"- {s['name']}: {s['description']}" for s in skills)

    def _path(self, name: str, pending: bool = False) -> str | None:
        if not isinstance(name, str) or not SKILL_NAME_RE.match(name):
            return None
        return os.path.join(self.pending_dir if pending else self.root, name + ".md")

    def read(self, name: str, pending: bool = False) -> str | None:
        path = self._path(name, pending)
        if path is None or not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return f.read()

    def save_draft(self, name: str, text: str) -> str:
        """Save a skill proposed by the agent into pending/ for a human to review."""
        path = self._path(name, pending=True)
        if path is None:
            raise ValueError("skill names are lowercase letters, digits, _ or -, up to 48 characters")
        meta, body = parse(text)
        if not body:
            raise ValueError("the skill has no body")
        meta = {**meta, "name": name}
        meta.setdefault("description", "")
        os.makedirs(self.pending_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n\n" + body + "\n")
        return path

    def approve(self, name: str) -> str:
        source = self._path(name, pending=True)
        if source is None or not os.path.exists(source):
            raise ValueError(f"no pending skill {name!r}")
        target = self._path(name)
        shutil.move(source, target)
        return target

    def reject(self, name: str):
        source = self._path(name, pending=True)
        if source is None or not os.path.exists(source):
            raise ValueError(f"no pending skill {name!r}")
        os.remove(source)
