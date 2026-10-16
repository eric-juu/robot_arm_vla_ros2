"""
Named arm poses. Defaults ship in config/poses.yaml. Poses saved from the web UI go to a user file
(default ~/.k1_web/poses.yaml) that survives rebuilds and overrides defaults with the same name.

File format:
    poses:
      zero:
        joints: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]   # 6 arm joints, radians
        gripper: 1.0                             # optional, 1.0 open .. 0.0 closed
"""
import os
import re
import threading

import yaml

from k1_web.control import CommandError, validate_positions

POSE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _read(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    poses = {}
    for name, pose in (data.get("poses") or {}).items():
        joints = validate_positions(pose["joints"])
        gripper = pose.get("gripper")
        poses[str(name)] = {"joints": joints, "gripper": None if gripper is None else float(gripper)}
    return poses


class PoseStore:

    def __init__(self, defaults_path: str, user_path: str):
        self.user_path = os.path.expanduser(user_path) if user_path else ""
        self._lock = threading.Lock()
        self._user = _read(self.user_path)
        self._poses = {**_read(defaults_path), **self._user}

    def names(self) -> list[str]:
        with self._lock:
            return list(self._poses)

    def get(self, name: str) -> dict:
        with self._lock:
            if name not in self._poses:
                raise CommandError(f"unknown pose {name!r}")
            return self._poses[name]

    def save(self, name: str, joints, gripper: float | None):
        if not isinstance(name, str) or not POSE_NAME_RE.match(name):
            raise CommandError("pose name must be 1-32 letters, digits, '_' or '-'")
        if not self.user_path:
            raise CommandError("saving poses is disabled (user_poses_file is empty)")
        pose = {"joints": [round(j, 4) for j in validate_positions(joints)],
                "gripper": None if gripper is None else round(float(gripper), 3)}
        with self._lock:
            user = {**self._user, name: pose}
            os.makedirs(os.path.dirname(self.user_path) or ".", exist_ok=True)
            tmp = self.user_path + ".tmp"
            with open(tmp, "w") as f:
                yaml.safe_dump({"poses": user}, f, sort_keys=False)
            os.replace(tmp, self.user_path)
            self._user = user
            self._poses[name] = pose
