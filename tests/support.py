"""Build the production project frontend with isolated state for tests."""

from types import SimpleNamespace
from chert.discord.frontend import Frontend
from chert.projects import Project, ProjectStore


def make_frontend(config, options, store, claude_channel_id=200, primary_id=100):
    projects = ProjectStore(config.state_file.parent / "projects.json")
    projects.guild_id = 1
    projects.projects["primary"] = Project("primary", str(config.project_root), primary_id)
    if claude_channel_id:
        projects.projects["secondary"] = Project(
            "secondary", str(config.project_root / "secondary"), claude_channel_id, "claude"
        )
    frontend = Frontend(config, options, store, projects=projects)
    frontend.owner = frontend.codex.owner = config.owner_id
    frontend.project_channels = {
        p.channel_id: SimpleNamespace(id=p.channel_id, parent_id=None)
        for p in projects.projects.values()
    }
    frontend.main_channel = frontend.codex.main_channel = frontend.project_channels[primary_id]
    return frontend
