"""Bounded, read-only project access; also executed over SSH as a stdlib script."""

import base64
import json
import os
from pathlib import Path
import stat
import sys

MAX_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 2000
HIDDEN = {"private", "node_modules", "__pycache__", "venv", "bot_state.json"}


def visible(path):
    return all(not part.startswith(".") and part not in HIDDEN
               and not part.lower().endswith((".pem", ".key", ".secret"))
               for part in path.parts)


def access(root, path="", action="list"):
    root = Path(root).resolve(strict=True)
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts or not visible(relative):
        raise PermissionError("This path is not available in the viewer.")
    target = (root / relative).resolve(strict=True)
    if not target.is_relative_to(root) or not visible(target.relative_to(root)):
        raise PermissionError("This path is outside the project or private.")
    if action == "list":
        entries = []
        truncated = False
        with os.scandir(target) as scan:
            for child in scan:
                if not visible(Path(child.name)) or child.is_symlink():
                    continue
                try:
                    info = child.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    continue
                if len(entries) == MAX_ENTRIES:
                    truncated = True
                    break
                entries.append({"name": child.name, "directory": stat.S_ISDIR(info.st_mode),
                                "size": info.st_size, "modified": info.st_mtime})
        entries.sort(key=lambda item: (not item["directory"], item["name"].casefold()))
        return {"entries": entries, "truncated": truncated}
    if action != "read":
        raise ValueError("Unknown file operation.")
    # O_NONBLOCK prevents special files from hanging a dashboard worker.
    fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Only regular files can be opened.")
        if info.st_size > MAX_BYTES:
            raise ValueError("Files up to 8 MB can be opened or downloaded.")
        data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("Files up to 8 MB can be opened or downloaded.")
    return {"data": base64.b64encode(data).decode(), "modified": info.st_mtime,
            "size": len(data)}


if __name__ == "__main__":
    try:
        print(json.dumps({"result": access(**json.load(sys.stdin))}))
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}))
