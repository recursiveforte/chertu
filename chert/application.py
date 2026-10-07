"""Production application assembly and single-instance lifetime."""

import logging
import os
from pathlib import Path
from chert.config import Config, CodexOptions
from chert.backends.codex.state import SessionStore
from chert.discord.frontend import Frontend
from chert.vendor import bridge as upstream


def main():
    config = Config.from_env()
    options = CodexOptions.from_env()
    # State remains compatible with both original installations.
    upstream.load_state()
    if upstream.ash_twin is not None:
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        upstream.ash_twin.PROTECTED.add(home)
        upstream.ash_twin.BACKUP_SETS.extend(
            [
                (home / "sessions", True, "*.jsonl"),
                (home / "archived_sessions", True, "*.jsonl"),
                (home / "config.toml", False, None),
                (home / "AGENTS.md", False, None),
                (config.state_file.resolve(), False, None),
                (config.state_file.resolve().parent / "codex-transcripts", True, "*.jsonl"),
            ]
        )
    logging.basicConfig(level=logging.INFO)
    import fcntl

    config.state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(str(config.state_file) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        Frontend(config, options, SessionStore(config.state_file)).run(config.token)
