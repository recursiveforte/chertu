"""Fresh session workspaces; existing sessions retain their saved cwd."""

import asyncio
from pathlib import Path
import subprocess
import uuid


def git(directory, *args):
    result = subprocess.run(
        ["git", "-C", str(directory), *args], capture_output=True, text=True, timeout=60
    )
    if result.returncode:
        raise ValueError(f"Could not prepare worktree: {result.stderr.strip()[:1200]}")
    return result.stdout.strip()


def validate_repository(directory):
    root = Path(git(directory, "rev-parse", "--show-toplevel")).resolve()
    # Resolve once so a concurrent checkout cannot change the starting revision.
    revision = git(directory, "rev-parse", "--verify", "HEAD^{commit}")
    return root, revision


def create_worktree(store, project):
    directory = Path(project.directory)
    root, revision = validate_repository(directory)
    relative = directory.relative_to(root)
    # A project may point into a repository rather than at its root. An
    # untracked subdirectory would not exist in a fresh worktree.
    if (
        relative != Path(".")
        and git(directory, "cat-file", "-t", f"{revision}:{relative}") != "tree"
    ):
        raise ValueError("The project directory must be committed before using worktrees.")
    identifier = uuid.uuid4().hex
    destination = store.worktree_root(project) / identifier
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    git(directory, "worktree", "add", "-b", f"chert/{identifier}", str(destination), revision)
    return destination / relative


async def session_directory(store, project, override=None):
    enabled = project.worktrees if override is None else override
    if not enabled:
        return Path(project.directory)
    return await asyncio.to_thread(create_worktree, store, project)
