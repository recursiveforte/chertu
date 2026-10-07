"""Persistent native Codex session mappings, including legacy state compatibility."""
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from state_io import write_json


@dataclass
class Session:
    discord_thread: int
    cwd: str
    name: str
    codex_thread: str | None = None
    model: str = ''
    effort: str = ''
    status: str = 'idle'
    turns: int = 0
    backend: str = 'exec'
    seen_live_items: list[str] = field(default_factory=list)
    active_turn: str | None = None
    status_message: int | None = None
    status_webhook: bool = False
    source_message: int | None = None
    turn_started: float = 0
    display_model: str = ''
    muted: bool = False
    permission_mode: str = 'default'
    service_tier: str | None = None
    deadline: dict | None = None
    recent: list[dict] = field(default_factory=list)
    collaboration_mode: str | None = None
    activity: dict = field(default_factory=dict)
    subagents: dict = field(default_factory=dict)
    journal_seen: list[str] = field(default_factory=list)
    last_completed_at: float = 0
    terminal_pane: str | None = None
    terminal_command: str = ''
    native_settings: bool = False
    pending_settings: dict = field(default_factory=dict)
    ended_seen_absent: bool = False
    mirror_since: float = 0
    mirrored_turns: list[str] = field(default_factory=list)
    delivery_failed: bool = False
    thread_title_cache: str = ''
    ended_at: float = 0


class SessionStore:
    def __init__(self, path: Path):
        self.path = path
        self.sessions: dict[int, Session] = {}
        self.meta = {}
        backup = path.with_suffix(path.suffix + '.bak')
        if path.exists() or backup.exists():
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                # Match upstream's last-good backup recovery. With no good backup,
                # fail visibly rather than replacing the session map with an empty one.
                data = json.loads(backup.read_text())
            if data.get('version') != 1:
                raise ValueError(f'Unsupported session state version in {path}')
            self.meta = data.get('meta', {})
            for row in data['sessions']:
                session = Session(**row)
                if session.status == 'running':
                    session.status = 'interrupted'
                self.sessions[session.discord_thread] = session

    def save(self):
        write_json(self.path, {'version': 1, 'meta': self.meta,
                              'sessions': [asdict(s) for s in self.sessions.values()]}, backup=True)
