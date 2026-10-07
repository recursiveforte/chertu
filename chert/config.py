"""Configuration shared by the Discord frontend and native Codex adapter."""

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
from chert.paths import runtime_path

EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")


@dataclass
class Config:
    token: str
    guild_id: int
    owner_id: int
    allowed_users: set[int]
    project_root: Path
    state_file: Path
    model: str = ""
    effort: str = ""
    discover: bool = True
    live_socket: Path | None = None
    discovery_interval: float = 5

    @classmethod
    def from_env(cls):
        token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
        guild = int(os.environ.get("DISCORD_GUILD_ID") or 0)
        if not token or not guild:
            raise ValueError("Run setup_discord.py to configure the Discord token and server.")
        root = (
            Path(os.environ.get("PROJECT_ROOT") or str(Path.home() / "projects"))
            .expanduser()
            .resolve()
        )
        if not root.is_dir():
            raise ValueError(f"PROJECT_ROOT does not exist: {root}")
        return cls(
            token,
            guild,
            int(os.environ.get("DISCORD_OWNER_ID") or 0),
            {
                int(x.strip())
                for x in os.environ.get("SPAWN_ALLOW_USERS", "").split(",")
                if x.strip()
            },
            root,
            runtime_path(os.environ.get("CODEX_STATE_FILE"), "private/codex-state.json"),
            os.environ.get("CODEX_MODEL", "").strip(),
            os.environ.get("CODEX_EFFORT", "").strip(),
            os.environ.get("CODEX_DISCOVER", "1") != "0",
            Path(
                os.environ.get("CODEX_APP_SERVER_SOCKET")
                or str(
                    Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
                    / "app-server-control/app-server-control.sock"
                )
            ).expanduser(),
            max(1, float(os.environ.get("CODEX_DISCOVERY_INTERVAL") or 5)),
        )


@dataclass
class CodexOptions:
    binary: str = "codex"
    sandbox: str = "workspace-write"
    network: bool = False

    def __post_init__(self):
        if self.sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError("Invalid CODEX_SANDBOX")

    @classmethod
    def from_env(cls):
        return cls(
            os.environ.get("CODEX_BIN")
            or shutil.which("codex")
            or str(Path.home() / ".local/bin/codex"),
            os.environ.get("CODEX_SANDBOX") or "workspace-write",
            os.environ.get("CODEX_NETWORK_ACCESS", "0") == "1",
        )
