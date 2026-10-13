"""Durable project/channel bindings, independent of either agent harness."""

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from chert.persistence import write_json


@dataclass
class Project:
    name: str
    directory: str
    channel_id: int
    harness: str = "codex"
    archived: bool = False
    worktrees: bool = False
    host: str = "local"
    fast_default: bool | None = None

    def __post_init__(self):
        if self.host == "local":
            self.directory = str(Path(self.directory).expanduser().resolve())
        elif not Path(self.directory).is_absolute():
            raise ValueError("Remote project directories must be absolute.")
        if self.harness not in {"codex", "claude"}:
            raise ValueError(f"Unknown harness for project {self.name}: {self.harness}")

    @property
    def topic(self):
        workspace = "worktree" if self.worktrees else "project directory"
        fast = "inherit" if self.fast_default is None else "on" if self.fast_default else "off"
        return (
            f"Project: {self.name} · Host: {self.host} · {self.directory} · New threads: {workspace}"
            f" · Codex fast default: {fast} (/fast-default)"
            " · /worktrees to change · /worktree or /no-worktree to override"
            " · /harness to choose an agent · /archive or /unarchive"
        )


class ProjectStore:
    def __init__(self, path):
        self.path = Path(path)
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.guild_id = int(data.get("guild_id", 0))
        self.category_id = int(data.get("category_id", 0))
        self.archive_category_id = int(data.get("archive_category_id", 0))
        self.projects = {p.name: p for p in (Project(**row) for row in data.get("projects", []))}

    def save(self):
        write_json(
            self.path,
            {
                "guild_id": self.guild_id,
                "category_id": self.category_id,
                "archive_category_id": self.archive_category_id,
                "projects": [asdict(p) for p in self.projects.values()],
            },
            indent=2,
        )

    def for_channel(self, channel):
        channel_id = getattr(channel, "parent_id", None) or getattr(channel, "id", None)
        return next((p for p in self.projects.values() if p.channel_id == channel_id), None)

    def for_directory(self, directory, host="local"):
        if not directory:
            return None
        directory = Path(directory).expanduser().resolve() if host == "local" else Path(directory)
        # Managed worktrees can live outside the source project (or inside a
        # different project, e.g. Chert's own private runtime directory).
        for project in self.projects.values():
            if project.host != host or host != "local":
                continue
            if directory.is_relative_to(self.worktree_root(project)):
                return project
        matches = [p for p in self.projects.values()
                   if p.host == host and directory.is_relative_to(Path(p.directory))]
        return max(matches, key=lambda p: len(Path(p.directory).parts), default=None)

    def worktree_root(self, project):
        return (self.path.parent / "worktrees" / str(project.channel_id)).resolve()

    def validate(self, name, directory, root, host="local"):
        name = name.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", name):
            raise ValueError(
                "Use a project name with 1–100 lowercase letters, numbers, hyphens or underscores."
            )
        if name in self.projects:
            raise ValueError(
                f"Project {name!r} already exists. Use /unarchive to reopen an archived project."
            )
        path = Path(directory)
        if host == "local":
            path = path.expanduser()
            path = (Path(root) / path if not path.is_absolute() else path).resolve()
        elif not path.is_absolute():
            raise ValueError("Resolve the directory on the remote host first.")
        if host == "local" and not path.is_dir():
            raise ValueError(f"Project directory does not exist on the bot host: {path}")
        if any(p.directory == str(path) and p.host == host for p in self.projects.values()):
            raise ValueError("That directory already has a project channel.")
        return name, str(path)

    def prepare(self, name, directory, root, harness="codex", host="local"):
        name, directory = self.validate(name, directory, root, host)
        return Project(name, directory, 0, harness, host=host)


async def create_project_channel(store, project, guild, category):
    """Use one channel-creation and persistence path for setup and /project."""
    channel = await guild.create_text_channel(project.name, category=category, topic=project.topic)
    project.channel_id = channel.id
    store.projects[project.name] = project
    store.save()
    return channel
