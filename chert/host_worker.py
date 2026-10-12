"""Stdlib-only SSH helper. Executed on the selected host, never the gateway.

The gateway sends this source and a JSON request over authenticated SSH. No bot
token, Chert installation, public listener, or second Codex daemon is needed.
"""

import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid


def execute(request):
    operation, params = request["operation"], request["params"]
    if request.get("path"):
        os.environ["PATH"] = request["path"]
    runtime = Path(request["runtime"]).expanduser().resolve()

    def directory(value):
        path = Path(value).expanduser()
        root = Path(request["root"] or "~/projects").expanduser()
        path = (path if path.is_absolute() else root / path).resolve()
        if not path.is_dir():
            raise ValueError(f"Project directory does not exist: {path}")
        return path

    def git(path, *args):
        proc = subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                              text=True, timeout=60)
        if proc.returncode:
            raise ValueError(proc.stderr.strip()[:1200])
        return proc.stdout.strip()

    if operation == "directory":
        return str(directory(params["directory"]))
    if operation in {"repository", "worktree"}:
        path = directory(params["directory"])
        root = Path(git(path, "rev-parse", "--show-toplevel")).resolve()
        revision = git(path, "rev-parse", "--verify", "HEAD^{commit}")
        if operation == "repository":
            return str(root)
        relative = path.relative_to(root)
        if relative != Path(".") and git(path, "cat-file", "-t", f"{revision}:{relative}") != "tree":
            raise ValueError("The project directory must be committed before using worktrees.")
        identifier = uuid.uuid4().hex
        dest = runtime / "worktrees" / str(int(params["channel_id"])) / identifier
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        git(path, "worktree", "add", "-b", f"chert/{identifier}", str(dest), revision)
        return str(dest / relative)
    if operation == "upload":
        data = base64.b64decode(params["data"], validate=True)
        if len(data) > 25 * 1024 * 1024:
            raise ValueError("Attachment exceeds 25 MB.")
        name = re.sub(r"[^A-Za-z0-9._-]", "_", params["name"])[:80] or "upload"
        folder = runtime / "attachments"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / f"{uuid.uuid4().hex}-{name}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        return str(path)
    if operation == "tmux":
        proc = subprocess.run(["tmux", *params["args"]], capture_output=True,
                              text=True, timeout=15)
        return [proc.returncode, proc.stdout.strip(), proc.stderr.strip()]
    if operation == "download":
        path = Path(params["path"]).expanduser()
        if not path.is_absolute():
            path = directory(params["cwd"]) / path
        with path.open("rb") as source:
            data = source.read(25 * 1024 * 1024 + 1)
        if len(data) > 25 * 1024 * 1024:
            raise ValueError("File exceeds 25 MB.")
        return {"name": path.name, "data": base64.b64encode(data).decode()}
    if operation == "info":
        return {"home": str(Path.home()), "runtime": str(runtime),
                "path": os.environ.get("PATH", "/usr/bin:/bin")}
    raise ValueError("Unknown host operation")


if __name__ == "__main__":
    try:
        print(json.dumps({"result": execute(json.load(sys.stdin))}))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
