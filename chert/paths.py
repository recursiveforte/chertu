"""Deployment paths are independent of module locations and the current directory."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def runtime_path(value, default):
    path = Path(value or default).expanduser()
    return path if path.is_absolute() else ROOT / path
