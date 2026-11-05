"""Tell conversational agents how to use the gateway's project lifecycle API."""

from pathlib import Path
import shlex

from chert.vendor import bridge as upstream


def project_context(host):
    if host != "local":
        return (
            "[Chert project context: /project name:NAME creates a new folder and Discord "
            "project channel; host selects the execution host (default local). "
            "Optional dir registers an existing directory. This session runs on host "
            f"{host}; the conversational project helper runs only on the Chert gateway. "
            "Do not edit Chert's project registry directly.]"
        )
    helper = Path(__file__).resolve().parents[3] / "bin/chert-project"
    command = shlex.join([
        "python3", str(helper), "--port", str(upstream.HOOK_PORT),
        "--secret-file", str(upstream.HOOK_SECRET_FILE),
    ])
    return (
        "[Chert project context: When the user asks you to create a Chert project, "
        f"run `{command} create NAME` using the shell. It creates a fresh folder under "
        "PROJECT_ROOT and a Discord project channel. Optional --host HOST selects a "
        "configured execution host; --dir PATH registers an existing directory instead. "
        f"Run `{command} list` to inspect existing projects and hosts. "
        "The helper authenticates privately; never print the secret or edit registry files. "
        "Use the returned directory for project files and share the returned channel URL. "
        "Creating a project does not move this conversation. Only create projects when "
        "the user asks; do not create one merely to answer a question.]"
    )
