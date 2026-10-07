"""The supported Discord command schema and harness dispatch callbacks."""

import functools
import logging
import discord

LOG = logging.getLogger(__name__)
REMOVED_COMMANDS = frozenset({"all", "hub", "restartall", "reviveall", "cleanup"})


def help_text(tree):
    lines = [
        "**Chert projects**",
        "Type a prompt in a project channel to start; reply in its thread to continue.",
    ]
    lines.extend(f"`/{command.name}` — {command.description}" for command in tree.get_commands())
    return "\n".join(lines)


def install_session_commands(frontend):
    # Keep upstream's descriptions, parameters, defaults, permission metadata,
    # autocomplete, and choices. Only replace its callback with channel routing.
    for command in frontend.tree.get_commands():
        if command.name in REMOVED_COMMANDS:
            frontend.tree.remove_command(command.name)
            continue
        original, name = command.callback, command.name

        def routed_callback(callback, command_name):
            @functools.wraps(callback)
            async def routed(interaction, **kwargs):
                return await frontend.dispatch_command(command_name, callback, interaction, kwargs)

            return routed

        command._callback = routed_callback(original, name)
        if name == "fork":
            command._params["to"].default = "same"
            command._params["to"].choices = [
                discord.app_commands.Choice(name=label, value=value)
                for label, value in [
                    ("same backend", "same"),
                    ("Claude", "claude"),
                    ("Codex", "codex"),
                ]
            ]
            command._params[
                "to"
            ].description = "Fork in the same backend, or hand the conversation to Claude/Codex"
        if name in {"model", "globalmodel"}:
            command._params[
                "name"
            ].description = "Model ID or alias; autocomplete follows this channel’s backend"
        if name == "model":
            command._params["name"].required = False
            command._params["name"].default = ""
            command._params[
                "name"
            ].description = "Model ID or alias; leave blank to open the model picker"
        if name in {"claude", "astra"}:
            command.description = (
                f"Launch a {'Claude' if name == 'claude' else 'Codex'} session in this project"
            )
            command._params.pop("project", None)
            command._params["prompt"].description = "What the agent should do in this project"
        if name not in {"claude", "astra"}:
            command.description = (
                command.description.replace("claude's", "session's")
                .replace("claudes", "sessions")
                .replace("claude", "session")[:100]
            )
        # Autocomplete must use the selected backend, too.
        for param in command._params.values():
            if param.autocomplete is not None:
                original_auto = param.autocomplete

                def autocomplete_router(callback, command_name):
                    @functools.wraps(callback)
                    async def routed(interaction, current):
                        if frontend.backend_for(interaction.channel) == "codex":
                            return await frontend.codex.controls.autocomplete(command_name, current)
                        return await callback(interaction, current)

                    return routed

                param.autocomplete = autocomplete_router(original_auto, name)

    @frontend.tree.command(name="codex", description="Launch a Codex session in this project")
    @discord.app_commands.describe(prompt="What the agent should do in this project")
    async def codex(interaction: discord.Interaction, prompt: str):
        await frontend.dispatch_command("codex", None, interaction, {"prompt": prompt})

    @frontend.tree.command(
        name="stop", description="Interrupt the active turn without ending the conversation"
    )
    async def stop(interaction: discord.Interaction):
        name = "stop" if frontend.backend_for(interaction.channel) == "codex" else "key"
        await frontend.dispatch_command(
            name,
            frontend.original_commands.get(name),
            interaction,
            {} if name == "stop" else {"key": "esc"},
        )

    @frontend.tree.error
    async def error(interaction, exc):
        LOG.error("Chert command failed: %s", exc, exc_info=exc)
        text = str(getattr(exc, "original", exc))[:1800]
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
