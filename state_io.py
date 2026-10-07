"""Atomic JSON persistence for project and session state."""
import json
import os
from pathlib import Path
import shutil
import tempfile


def write_json(path, data, *, backup=False, indent=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as file:
            json.dump(data, file, indent=indent)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        if backup and path.exists():
            try:
                json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                pass  # Never replace the last good backup with a corrupt file.
            else:
                previous = path.with_suffix(path.suffix + '.bak')
                shutil.copyfile(path, previous)
                os.chmod(previous, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
