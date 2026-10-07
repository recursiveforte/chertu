"""Claude adapter: delegate behavior to the unmodified upstream implementation."""

import asyncio
from types import SimpleNamespace

from chert.vendor import bridge as upstream


class ClaudeBackend:
    name = "claude"

    def __init__(self, frontend, enabled=True):
        self.frontend = frontend
        self.enabled = enabled

    @property
    def active_count(self):
        return (
            sum(not saved.get("ended") for saved in upstream.sessions_state().values())
            if self.enabled
            else 0
        )

    def session_lines(self, project):
        lines = []
        for saved in upstream.sessions_state().values():
            if saved.get("parent") == project.channel_id and saved.get("thread"):
                status = "ended" if saved.get("ended") else saved.get("status", "idle")
                lines.append(
                    f"Claude · {status} · **{saved.get('name', 'session')}** → <#{saved['thread']}>"
                )
        return lines

    async def adopt_attached(self, session, message):
        """Reuse upstream's registration/recovery logic with a message-owned thread."""

        async def create_thread(**kwargs):
            kwargs.pop("type", None)  # Message.create_thread derives its type from the parent.
            return await message.create_thread(**kwargs)

        parent = SimpleNamespace(id=message.channel.id, create_thread=create_thread)
        # The source message already announces the session. Upstream's adopter
        # needs only thread lookup when reusing a prior mapping; skip its extra announcement.
        host = SimpleNamespace(get_thread=self.frontend.get_thread, main_channel=None)
        return await upstream.Bridge.adopt_session(host, session, parent, {"spawned": True})

    def owns_thread(self, channel_id):
        return channel_id in upstream.thread_to_key()

    async def message(self, message):
        host = self.frontend
        content = (message.content or "").strip()
        control = content.split(maxsplit=1)[0].lower() if content else ""
        if not host.claude_enabled and control not in {
            "!help",
            "!sessions",
            "!status",
            "!threads",
            "!ls",
            "!disk",
            "!s3",
            "!backup",
            "!offload",
            "!restore",
            "!astra",
        }:
            return await host.say(message.channel, "Claude is disabled. Use /harness codex.")
        project = host.projects.for_channel(message.channel)
        if project and message.channel.id == project.channel_id:
            return await host.handle_main_command(message, content)
        return await upstream.Bridge.on_message(host, message)

    async def command(self, name, original, interaction, kwargs):
        host = self.frontend
        if not host.claude_enabled and name not in {
            "help",
            "sessions",
            "disk",
            "s3",
            "backup",
            "offload",
            "restore",
        }:
            return await interaction.response.send_message(
                "Claude is disabled. Use /harness codex.", ephemeral=True
            )
        if name == "fork" and kwargs.get("to") == "codex":
            kwargs = {**kwargs, "to": "astra"}
        return await original(interaction, **kwargs)

    async def launch(self, project, prompt, user, respond, source=None):
        if not self.frontend.claude_enabled:
            return await respond(
                "Claude is disabled. Use /harness codex or enable Claude on the bot host."
            )
        async with self.frontend.project_lock:
            if source:
                await source.add_reaction("🚀")
            pane, error = await asyncio.to_thread(
                upstream.spawn_claude, upstream.slug(prompt, 32), project.directory
            )
            if not pane:
                return await respond(f"Could not launch Claude: {error}")
            session, notes = await self.frontend.await_registration(pane)
            if not session:
                return await respond(
                    await self.frontend.spawn_failure_text(pane, project.directory, notes)
                )
            # Both launch and discovery use this lock so only one owns thread creation.
            if source:
                thread = await self.adopt_attached(session, source)
            else:
                thread = await self.frontend.adopt_session(
                    session, self.frontend.main_channel, {"spawned": True}
                )
            ok, error = await asyncio.to_thread(self.frontend.deliver, session, user, prompt)
            if source and ok:
                await source.add_reaction("📡")
            elif not source:
                await respond(
                    f"Claude → {thread.mention}"
                    if thread
                    else "Claude launched; waiting for its thread."
                )
            if not ok:
                await respond(f"Prompt was not delivered: {error}")
            return thread
