"""Persistent native Codex session mappings, including legacy state compatibility."""

from dataclasses import asdict, dataclass, field
from collections.abc import MutableMapping
import json
import time

import discord
from pathlib import Path
from chert.persistence import write_json


def session_key(session):
    return session.codex_thread if session.host == "local" else f"{session.host}~{session.codex_thread}"


@dataclass
class Session:
    discord_thread: int
    cwd: str
    name: str
    codex_thread: str | None = None
    model: str = ""
    effort: str = ""
    status: str = "idle"
    turns: int = 0
    backend: str = "exec"
    seen_live_items: list[str] = field(default_factory=list)
    active_turn: str | None = None
    status_message: int | None = None
    status_webhook: bool = False
    source_message: int | None = None
    turn_started: float = 0
    display_model: str = ""
    muted: bool = False
    permission_mode: str = "default"
    service_tier: str | None = None
    deadline: dict | None = None
    recent: list[dict] = field(default_factory=list)
    collaboration_mode: str | None = None
    activity: dict = field(default_factory=dict)
    subagents: dict = field(default_factory=dict)
    journal_seen: list[str] = field(default_factory=list)
    last_interaction_at: float = field(default_factory=time.time)
    last_completed_at: float = 0
    terminal_pane: str | None = None
    terminal_command: str = ""
    native_settings: bool = False
    pending_settings: dict = field(default_factory=dict)
    ended_seen_absent: bool = False
    mirror_since: float = 0
    mirrored_turns: list[str] = field(default_factory=list)
    delivery_failed: bool = False
    thread_title_cache: str = ""
    ended_at: float = 0
    prompt_messages: list[dict] = field(default_factory=list)
    host: str = "local"
    project_channel: int | None = None


class SessionStore:
    def __init__(self, path: Path):
        self.path = path
        self.sessions: dict[int, Session] = {}
        self.meta = {}
        backup = path.with_suffix(path.suffix + ".bak")
        if path.exists() or backup.exists():
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                # Match upstream's last-good backup recovery. With no good backup,
                # fail visibly rather than replacing the session map with an empty one.
                data = json.loads(backup.read_text())
            if data.get("version") != 1:
                raise ValueError(f"Unsupported session state version in {path}")
            self.meta = data.get("meta", {})
            for row in data["sessions"]:
                # Recover an existing session's clock without making a bridge
                # restart count as interaction. Old state has no dedicated clock.
                row.setdefault("last_interaction_at", max(
                    row.get("last_completed_at", 0),
                    row.get("turn_started", 0),
                    *(item.get("time", 0) for item in row.get("recent", [])),
                    discord.utils.snowflake_time(row["discord_thread"]).timestamp(),
                ))
                session = Session(**row)
                if session.status == "running":
                    session.status = "interrupted"
                self.sessions[session.discord_thread] = session

    def save(self):
        write_json(
            self.path,
            {
                "version": 1,
                "meta": self.meta,
                "sessions": [asdict(s) for s in self.sessions.values()],
            },
            backup=True,
        )


class HostSessions(MutableMapping):
    """A live host-scoped view; every save still writes the complete state."""

    def __init__(self, store, host):
        self.store, self.host = store, host

    def __getitem__(self, key):
        session = self.store.sessions[key]
        if session.host != self.host:
            raise KeyError(key)
        return session

    def __setitem__(self, key, session):
        existing = self.store.sessions.get(key)
        if existing and existing.host != self.host:
            raise ValueError("A Discord thread is already owned by another host.")
        session.host = self.host
        self.store.sessions[key] = session

    def __delitem__(self, key):
        self[key]
        del self.store.sessions[key]

    def __iter__(self):
        return iter([key for key, value in self.store.sessions.items() if value.host == self.host])

    def __len__(self):
        return sum(value.host == self.host for value in self.store.sessions.values())


class HostSessionStore:
    def __init__(self, store, host):
        self.sessions = HostSessions(store, host)
        self.meta = store.meta if host == "local" else store.meta.setdefault("hosts", {}).setdefault(host, {})
        self.path = store.path if host == "local" else store.path.parent / "hosts" / host / store.path.name
        self.save = store.save
