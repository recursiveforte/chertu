"""The shared /model picker; selections use the existing backend command path."""

import asyncio
import logging

import discord
from chert.vendor import bridge as upstream

LOG = logging.getLogger(__name__)


class ModelPicker(discord.ui.View):
    def __init__(self, frontend, interaction, backend, models, selected):
        super().__init__(timeout=300)
        self.frontend, self.backend = frontend, backend
        self.owner, self.channel_id = interaction.user.id, interaction.channel_id
        options = [
            discord.SelectOption(label=label[:100], value=value, default=value == selected)
            for label, value in models
            if value and len(value) <= 100
        ]
        # Keep the current/queued choice visible even with a large catalog.
        options.sort(key=lambda option: not option.default)
        self.select = discord.ui.Select(placeholder="Choose a model", options=options[:25])
        self.select.callback = self.choose
        self.add_item(self.select)

    async def interaction_check(self, interaction):
        if (
            interaction.user.id != self.owner
            or interaction.channel_id != self.channel_id
            or not self.frontend.allowed_user(interaction.user)
            or not self.frontend.privileged(interaction.user)
            or self.frontend.backend_for(interaction.channel) != self.backend
        ):
            await interaction.response.send_message(
                "This model picker belongs to another session or owner.", ephemeral=True
            )
            return False
        return True

    async def choose(self, interaction):
        value = self.select.values[0]
        if value not in {option.value for option in self.select.options}:
            return await interaction.response.send_message(
                "Choose a model from this picker.", ephemeral=True
            )
        await self.frontend.dispatch_command(
            "model", self.frontend.original_commands["model"], interaction, {"name": value}
        )
        await interaction.message.edit(content=f"Selected `{value}`.", view=None)
        self.stop()

    async def on_error(self, interaction, error, item):
        LOG.error("Model picker failed", exc_info=error)
        text = f"Could not select model: {error}"[:1900]
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


async def show_model_picker(frontend, interaction, backend):
    if backend == "claude" and not frontend.claude_enabled:
        return await interaction.response.send_message(
            "Claude is disabled. Use /harness codex.", ephemeral=True
        )
    if backend == "codex":
        session = frontend.codex.store.sessions.get(interaction.channel_id)
        if session is None:
            return await interaction.response.send_message(
                "Open a session thread to choose its model. Use `/globalmodel` to change the default for all sessions.",
                ephemeral=True,
            )
        if session.status == "ended":
            return await interaction.response.send_message(
                "Revive this session before choosing a model.", ephemeral=True
            )
        await interaction.response.defer(thinking=True, ephemeral=True)
        live = frontend.codex.live
        await live.connect()
        catalog, info = await asyncio.gather(
            live.call("model/list", {}),
            live.call("thread/read", {"threadId": session.codex_thread, "includeTurns": False}),
        )
        models = [(row.get("displayName") or row["model"], row["model"]) for row in catalog["data"]]
        current = info["thread"].get("model") or session.display_model or "default"
        pending = session.pending_settings.get("model")
        note = "Your selection applies to the next turn."
    else:
        key = upstream.thread_to_key().get(interaction.channel_id)
        if not key:
            return await interaction.response.send_message(
                "Open a session thread to choose its model.", ephemeral=True
            )
        await interaction.response.defer(thinking=True, ephemeral=True)
        state = upstream.state[key]
        current, pending = state.get("model") or "default", state.get("pending_model")
        models = [(model, model) for model in upstream.MODEL_SUGGESTIONS]
        note = "If Claude is busy, your selection is queued until it is idle."
    if not models:
        return await interaction.followup.send(
            "No models are available from this backend.", ephemeral=True
        )
    body = f"**Current model:** `{current}`"
    if pending:
        body += f"\n**Queued model:** `{pending}`"
    body += "\n" + note
    view = ModelPicker(frontend, interaction, backend, models, pending or current)
    await interaction.followup.send(
        body, view=view, ephemeral=True, allowed_mentions=upstream.NO_PING
    )
