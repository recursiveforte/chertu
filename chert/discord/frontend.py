"""One project-based Discord gateway composed with independent harness controllers."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import os
import re
from pathlib import Path

import discord

from chert.vendor import bridge as upstream
from chert.paths import runtime_path
from chert.projects import ProjectStore
from chert.discord.projects import ProjectCommands
from chert.discord.audio import AudioMessage, Transcriber, prepare_audio
from chert.backends.base import Harness
from chert.backends.codex.backend import CodexBackend
from chert.backends.claude import ClaudeBackend
from chert.discord.commands import REMOVED_COMMANDS, help_text, install_session_commands
from chert.web.bridge_api import BridgeAPI

LOG = logging.getLogger(__name__)


class Frontend(upstream.Bridge):
    def __init__(self, config, options, store, projects=None):
        self.config = config
        self.projects = projects or ProjectStore(
            runtime_path(os.environ.get("PROJECT_STATE_FILE"), "private/projects.json")
        )
        self._project_context = ContextVar("chert_project", default=None)
        self.project_lock = asyncio.Lock()
        self.project_channels = {}
        self.transcriber = Transcriber()
        upstream.CHANNEL_ID = 0
        upstream.PROJECT_ROOT = config.project_root
        super().__init__(intents=self.intents_for_bot())
        self.codex = CodexBackend(config, options, store, self)
        self.claude = ClaudeBackend(self, enabled=os.environ.get("CLAUDE_ENABLED", "1") != "0")
        self.backends: dict[str, Harness] = {"codex": self.codex, "claude": self.claude}
        self.api = BridgeAPI(self)
        self.original_commands = {
            c.name: c.callback for c in self.tree.get_commands() if c.name not in REMOVED_COMMANDS
        }
        install_session_commands(self)
        self.project_commands = ProjectCommands(self, self.project_lock)
        self.project_commands.install()

    @staticmethod
    def intents_for_bot():
        intents = discord.Intents.default()
        intents.message_content = True
        return intents

    async def on_hook(self, event):
        if self.claude_enabled:
            await super().on_hook(event)

    async def admin_restart_all(self, request):
        if not self.claude_enabled:
            from aiohttp import web

            return web.Response(status=503, text="Claude is disabled\n")
        return await super().admin_restart_all(request)

    @property
    def claude_enabled(self):
        return self.claude.enabled

    @claude_enabled.setter
    def claude_enabled(self, value):
        self.claude.enabled = value

    async def update_shared_presence(self):
        counts = [
            (name.title(), harness.active_count)
            for name, harness in self.backends.items()
            if harness.enabled
        ]
        text = f"🔭 {sum(count for _, count in counts)} travelers · " + " · ".join(
            f"{name} {count}" for name, count in counts
        )
        if text != getattr(self, "_shared_presence", None):
            await discord.Client.change_presence(self, activity=discord.CustomActivity(text))
            self._shared_presence = text

    async def change_presence(self, **kwargs):
        await self.update_shared_presence()

    async def on_message(self, message):
        if message.author.id == self.user.id or message.webhook_id:
            return
        if not self.allowed_user(message.author):
            return
        return await self.route_message(message)

    def allowed_user(self, user):
        return (
            user.id == (self.owner or self.codex.owner)
            or user.id in self.codex.config.allowed_users
        )

    async def dispatch_command(self, name, original, interaction, kwargs):
        if not self.allowed_user(interaction.user):
            return await interaction.response.send_message(
                "This Chert instance is restricted.", ephemeral=True
            )
        return await self.route_command(name, original, interaction, kwargs)

    async def astra_start(self, parent, user, cwd, prompt, respond, **kwargs):
        # Upstream's cross-backend handoff construction remains the source of truth;
        # only its destination changes from the old Astra sidecar to #codex.
        thread = await self.codex.start_session(prompt, cwd=Path(cwd))
        await respond(f"🚀 Codex → {thread.mention}")
        return thread

    async def close(self):
        await self.codex.shutdown()
        titles = list(self._title_tasks)
        for session in self.codex.store.sessions.values():
            if session.discord_thread in self._retitling:
                session.thread_title_cache = ""
        if titles:
            self.codex.store.save()
        for task in titles:
            task.cancel()
        await asyncio.gather(*titles, return_exceptions=True)
        poller = getattr(self, "poller", None)
        if poller:
            poller.cancel()
            await asyncio.gather(poller, return_exceptions=True)
        await self.api.close()
        await super().close()

    async def disk_tick(self):
        await upstream.Bridge.disk_tick(self)

    async def send_help(self, channel):
        for part in upstream.split_chunks(help_text(self.tree)):
            await self.say(channel, part)

    @property
    def current_project(self):
        return self._project_context.get()

    @contextmanager
    def project_context(self, project):
        token = self._project_context.set(project)
        try:
            yield
        finally:
            self._project_context.reset(token)

    @property
    def main_channel(self):
        project = self.current_project
        if project:
            return self.project_channels.get(project.channel_id)
        return getattr(self, "_main_channel", None)

    @main_channel.setter
    def main_channel(self, value):
        self._main_channel = value

    def backend_for(self, channel):
        project = self.projects.for_channel(channel)
        if project is None:
            return None
        for name, harness in self.backends.items():
            if harness.owns_thread(channel.id):
                return name
        # Unknown threads must never silently become a new session in another harness.
        return None if getattr(channel, "parent_id", None) else project.harness

    async def setup_hook(self):
        if self.projects.guild_id and self.projects.guild_id != self.config.guild_id:
            raise ValueError("Project registry and DISCORD_GUILD_ID refer to different servers.")
        guild_id = self.projects.guild_id or self.config.guild_id
        if not guild_id:
            raise ValueError("Run setup_discord.py to configure project channels.")
        guild = await self.fetch_guild(guild_id)
        self.owner = self.codex.owner = self.codex.config.owner_id or guild.owner_id
        for project in self.projects.projects.values():
            try:
                channel = await self.fetch_channel(project.channel_id)
            except discord.NotFound:
                LOG.warning(
                    "Project channel %s was deleted; run setup_discord.py to repair it.",
                    project.name,
                )
                continue
            if channel.guild.id != guild_id:
                raise ValueError(f"Project {project.name} is in another server.")
            self.project_channels[channel.id] = channel
        self.main_channel = next(
            (
                self.project_channels[p.channel_id]
                for p in self.projects.projects.values()
                if not p.archived and p.channel_id in self.project_channels
            ),
            next(iter(self.project_channels.values()), None),
        )
        await self.codex.start_backend()
        await self.api.start()
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.poller = asyncio.create_task(self.poll_loop())

    async def on_ready(self):
        LOG.info(
            "Project frontend online: %s projects in guild %s",
            len(self.projects.projects),
            self.projects.guild_id,
        )

    async def post_as(self, channel, name, content, thread_id=None, seed=None):
        # Session/project titles can contain Discord's reserved username text.
        # Adapt only the webhook identity; keep stored titles and avatar seeds.
        display_name = re.sub("discord", "chat", name, flags=re.IGNORECASE)
        return await super().post_as(
            channel, display_name, content, thread_id, seed=seed or name
        )

    async def poll_loop(self):
        """Poll Claude without upstream's auto-created harness channels or chat buses."""
        await self.wait_until_ready()
        if self.claude_enabled:
            upstream.migrate_state(await asyncio.to_thread(upstream.live_sessions))
            meta = upstream.state["_meta"]
            boot = upstream.boot_id()
            self._rebooted = bool(boot and meta.get("boot_id") and boot != meta["boot_id"])
            if boot:
                meta["boot_id"] = boot
                upstream.save_state()
        backlog, self._hook_backlog = self._hook_backlog, []
        for event in backlog:
            await self.on_hook(event)
        while not self.is_closed():
            steps = [self.disk_tick, self.update_shared_presence]
            if self.claude_enabled:
                steps += [self.run_tick, self.drain_spool, self.supernova_tick]
            for step in steps:
                try:
                    await step()
                except Exception:
                    LOG.exception("Project monitor failed: %s", step.__name__)
            await asyncio.sleep(upstream.POLL_SECS)

    async def open_thread(self, channel, session, key):
        project = self.projects.for_directory(session.get("cwd"))
        if project is None or project.archived or project.channel_id not in self.project_channels:
            return
        async with self.project_lock:
            with self.project_context(project):
                return await super().open_thread(self.main_channel, session, key)

    async def tick_session(self, channel, key, session, saved):
        project = self.projects.for_directory(saved.get("cwd") or session.get("cwd"))
        with self.project_context(project):
            return await super().tick_session(self.main_channel or channel, key, session, saved)

    async def route_message(self, message):
        project = self.projects.for_channel(message.channel)
        if project is None:
            return
        if project.archived:
            return await self.say(
                message.channel, "This project is archived. Use /unarchive to continue."
            )
        with self.project_context(project):
            content = (message.content or "").strip()
            command = content.split(maxsplit=1)[0].lower() if content else ""
            if command in {"!sessions", "!status", "!threads", "!ls"}:
                return await self.say(message.channel, self.project_sessions(project))
            if command in {
                "!all",
                "!hub",
                "!restartall",
                "!reviveall",
                "!cleanup",
            } or content.lower().startswith(("!restart all", "!revive all")):
                return await self.say(
                    message.channel,
                    "Harness-wide commands have been removed. Use the session’s thread to control it.",
                )
            if message.channel.id == project.channel_id:
                for mention in (f"<@{self.user.id}>", f"<@!{self.user.id}>"):
                    content = content.replace(mention, "")
                content = content.strip()
                harness = project.harness
                for prefix, selected in (
                    ("!codex ", "codex"),
                    ("!astra ", "codex"),
                    ("!claude ", "claude"),
                ):
                    if content.startswith(prefix):
                        harness, content = selected, content[len(prefix) :].strip()
                        break
                else:
                    if content.startswith("!"):
                        return await self.backends[harness].message(message)
                try:
                    return await self.launch_project_message(project, harness, message, content)
                except Exception as exc:
                    LOG.exception("Project session launch failed")
                    return await self.say(message.channel, f"Could not launch: {str(exc)[:1500]}")
            backend = self.backend_for(message.channel)
            if backend:
                try:
                    if not content.startswith("!"):
                        message = await prepare_audio(message, self.transcriber)
                        if isinstance(message, AudioMessage):
                            await message.publish(message.channel)
                    return await self.backends[backend].message(message)
                except Exception as exc:
                    LOG.exception("Session message handling failed")
                    return await self.say(message.channel, f"Could not deliver: {str(exc)[:1500]}")

    async def launch_project_message(self, project, harness, message, prompt):
        message = await prepare_audio(message, self.transcriber)
        if isinstance(message, AudioMessage):
            prompt = "\n\n".join(part for part in (prompt, message.transcript_text) if part)
        attached = await self.save_attachments(message)
        prompt = "\n".join(part for part in (prompt, attached) if part)
        if not prompt:
            return
        return await self.backends[harness].launch(
            project,
            prompt,
            message.author,
            lambda text: self.say(message.channel, text),
            source=message,
        )

    async def route_command(self, name, original, interaction, kwargs):
        project = self.projects.for_channel(interaction.channel)
        if project is None:
            return await interaction.response.send_message(
                "Use this command in a project channel. /project creates one.", ephemeral=True
            )
        if project.archived and name not in {"stop", "key", "kill", "log", "screen", "help"}:
            return await interaction.response.send_message(
                "This project is archived. Use /unarchive to continue.", ephemeral=True
            )
        with self.project_context(project):
            if name == "sessions":
                return await interaction.response.send_message(
                    self.project_sessions(project), allowed_mentions=upstream.NO_PING
                )
            if name in {"claude", "codex", "astra"}:
                await interaction.response.defer(thinking=True)

                async def respond(text):
                    return await interaction.followup.send(text, allowed_mentions=upstream.NO_PING)

                harness = "codex" if name == "astra" else name
                return await self.backends[harness].launch(
                    project, kwargs["prompt"], interaction.user, respond
                )
            backend = self.backend_for(interaction.channel)
            if backend is None:
                return await interaction.response.send_message(
                    "Use this command in a tracked session thread.", ephemeral=True
                )
            if name in {"model", "globalmodel", "fast"} and not self.privileged(interaction.user):
                return await interaction.response.send_message(
                    f"/{name} is owner-only.", ephemeral=True
                )
            if name == "model" and not kwargs.get("name", "").strip():
                from chert.discord.models import show_model_picker

                return await show_model_picker(self, interaction, backend)
            return await self.backends[backend].command(name, original, interaction, kwargs)

    def project_sessions(self, project):
        lines = [f"**{project.name}** · default harness: **{project.harness}**"]
        for harness in self.backends.values():
            lines.extend(harness.session_lines(project))
        return (
            "\n".join(lines)[:1900]
            if len(lines) > 1
            else lines[0] + "\nNo sessions yet. Type a prompt to start one."
        )

    async def deliver_session_file(self, path, caption, pane, sid, cwd, backend=None):
        session = next(
            (s for s in self.codex.store.sessions.values() if sid and s.codex_thread == sid), None
        )
        project = self.projects.for_directory(session.cwd if session else cwd)
        if project is None:
            raise ValueError("The session directory does not belong to a registered project.")
        with self.project_context(project):
            matches = [
                s
                for s in self.codex.store.sessions.values()
                if (sid and s.codex_thread == sid)
                or (not sid and cwd and s.cwd == cwd and s.status != "ended")
            ]
            if len(matches) == 1:
                session = matches[0]
                target = (
                    self.codex.main_channel
                    if session.status == "ended"
                    else await self.codex.live_channel(session)
                )
                if Path(path).stat().st_size > upstream.FILE_LIMIT_BYTES:
                    return await self.codex.say(target, f"File exceeds Discord limit: `{path}`")
                return await target.send(
                    caption or None, file=discord.File(path), allowed_mentions=upstream.NO_PING
                )
            if len(matches) > 1:
                raise ValueError(
                    "More than one Codex session uses that directory; provide its session ID."
                )
            if backend == "codex":
                if Path(path).stat().st_size > upstream.FILE_LIMIT_BYTES:
                    raise ValueError("File exceeds the Discord upload limit.")
                return await self.codex.main_channel.send(
                    caption or None, file=discord.File(path), allowed_mentions=upstream.NO_PING
                )
            return await upstream.Bridge.deliver_session_file(self, path, caption, pane, sid, cwd)

    async def on_interaction(self, interaction):
        if interaction.type == discord.InteractionType.component and not self.allowed_user(
            interaction.user
        ):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "This Chert instance is restricted.", ephemeral=True
                )
            return
        with self.project_context(self.projects.for_channel(interaction.channel)):
            if self.backend_for(interaction.channel) == "codex":
                return await self.codex.controls.on_component(interaction)
            return await super().on_interaction(interaction)
