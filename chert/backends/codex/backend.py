"""Codex harness lifecycle and its Discord-facing interface."""

import asyncio
import logging
from pathlib import Path
import time
import uuid

import discord

from chert.vendor import bridge as upstream
from chert.backends.codex import storage as codex_storage
from chert.backends.codex.terminal import CodexTerminal
from chert.backends.codex.state import Session
from chert.backends.codex.client import LiveCodex
from chert.backends.codex.events import CodexEvents
from chert.backends.codex.controls import CodexControls, text_arguments
from chert.backends.codex.discovery import CodexDiscovery
from chert.backends.codex.client import RpcError
from chert.backends.codex.presentation import prompt_name, speaker_name, thread_title

LOG = logging.getLogger(__name__)


class CodexBackend:
    name = "codex"
    enabled = True

    @property
    def active_count(self):
        return sum(s.status not in {"ended", "disconnected"} for s in self.store.sessions.values())

    def session_lines(self, project):
        return [
            f"Codex · {s.status} · **{s.name}** → <#{s.discord_thread}>"
            for s in self.store.sessions.values()
            if self.frontend.projects.for_directory(s.cwd) == project
        ]

    def owns_thread(self, channel_id):
        return channel_id in self.store.sessions

    async def command(self, name, original, interaction, arguments):
        await interaction.response.defer(thinking=True, ephemeral=name == "kill")

        async def respond(text):
            await interaction.followup.send(
                text, allowed_mentions=upstream.NO_PING, suppress_embeds=True, ephemeral=name == "kill"
            )

        return await self.controls.execute(
            name, interaction.channel, interaction.user, arguments, respond
        )

    async def launch(self, project, prompt, user, respond, source=None):
        thread = await self.start_session(
            prompt, source_message=source, cwd=Path(project.directory)
        )
        if source is None:
            await respond(f"Codex → {thread.mention}")
        return thread

    @property
    def main_channel(self):
        project = self.frontend.current_project
        if project:
            return self.frontend.project_channels.get(project.channel_id)
        return getattr(self, "_main_channel", None)

    @main_channel.setter
    def main_channel(self, channel):
        self._main_channel = channel

    def __init__(self, config, options, store, frontend):
        self.frontend, self.config, self.options, self.store = frontend, config, options, store
        self.main_channel = None
        self.owner = config.owner_id
        self.stopping = set()
        self.session_creation_lock = asyncio.Lock()
        self.live = LiveCodex(
            config.live_socket or Path.home() / ".codex/app-server-control/app-server-control.sock"
        )
        self.background_tasks = []
        self.events = CodexEvents(self)
        self.controls = CodexControls(self)
        self.discovery = CodexDiscovery(self)
        self.terminal = CodexTerminal(options.binary, self.live.socket)

    async def wait_until_ready(self):
        await self.frontend.wait_until_ready()

    def is_closed(self):
        return self.frontend.is_closed()

    async def start_backend(self):
        self.main_channel = self.frontend.main_channel
        self.owner = self.frontend.owner
        if self.main_channel is not None:
            await self.webhook_for()
        self.background_tasks = [
            asyncio.create_task(self.events.run()),
            asyncio.create_task(self.maintenance()),
            asyncio.create_task(self.events.activity_loop()),
        ]
        if self.config.discover:
            self.background_tasks.append(asyncio.create_task(self.discovery.run()))

    def allowed(self, user_id, channel):
        return bool(
            self.frontend.projects.for_channel(channel)
            and (user_id == self.owner or user_id in self.config.allowed_users)
        )

    async def shutdown(self):
        tasks = self.background_tasks
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.live.close()

    async def webhook_for(self, channel=None):
        return await self.frontend.webhook_for(channel or self.main_channel)

    async def discovery_channel(self, info):
        projects = self.frontend.projects
        project = projects.for_directory(info.get("cwd"))
        if project and not project.archived:
            return self.frontend.project_channels.get(project.channel_id)
        return None

    async def parent_channel(self, thread):
        return getattr(thread, "parent", None) or await self.fetch_channel(thread.parent_id)

    async def say(self, channel, text):
        session = self.store.sessions.get(getattr(channel, "id", None))
        if session is None:
            return await self.frontend.say(channel, str(text))
        name = speaker_name(session)
        entries = upstream.format_new_items(
            [{"kind": "assistant", "text": str(text)}],
            session.codex_thread or "",
            include_tools=False,
        )
        message = None
        for _, content in entries:
            if content.startswith("-# …truncated — "):
                content = content.replace(
                    upstream.DASHBOARD, upstream.DASHBOARD.removesuffix("/claudes") + "/codex"
                )
            message = await self.frontend.post_as(
                await self.parent_channel(channel),
                name,
                content,
                session.discord_thread,
                seed=str(session.discord_thread),
            )
        return message

    async def _start_session(self, prompt, *, codex_id=None, source_message=None, cwd=None):
        current = self.frontend.current_project
        if current and not codex_id and cwd is None:
            cwd = Path(current.directory)
        if source_message and source_message.id in self.store.sessions:
            return await self.fetch_channel(source_message.id)
        old = (
            next((s for s in self.store.sessions.values() if s.codex_thread == codex_id), None)
            if codex_id
            else None
        )
        if old:
            await self.ensure_live(old)
            thread = await self.live_channel(old)
            old.status = "idle" if old.status == "ended" else old.status
            self.store.save()
            return thread
        await self.live.connect()
        if codex_id:
            codex_id = str(uuid.UUID(codex_id))
            try:
                result = await self.live.call(
                    "thread/resume", {"threadId": codex_id, "excludeTurns": True}
                )
            except RpcError:
                if not await asyncio.to_thread(codex_storage.restore, codex_id):
                    raise
                result = await self.live.call(
                    "thread/resume", {"threadId": codex_id, "excludeTurns": True}
                )
        else:
            if cwd is None:
                raise ValueError("Start sessions from a registered project channel.")
            cwd = Path(cwd).expanduser().resolve()
            params = {
                "cwd": str(cwd),
                "approvalPolicy": "on-request",
                "sandbox": self.options.sandbox,
                "config": {"sandbox_workspace_write.network_access": self.options.network},
            }
            if upstream.PERMISSION_MODE == "auto":
                params["approvalsReviewer"] = "auto_review"
            if self.store.meta.get("yolo_until", 0) > time.time():
                params.update(approvalPolicy="never", sandbox="danger-full-access")
            model = self.store.meta.get("model", self.config.model)
            if model:
                params["model"] = model
            if self.config.effort:
                params["config"]["model_reasoning_effort"] = self.config.effort
            result = await self.live.call("thread/start", params)
        info = result["thread"]
        parent = self.main_channel
        projects = self.frontend.projects
        destination = projects.for_directory(info.get("cwd"))
        if destination is None or destination.archived:
            raise ValueError(
                "This session needs an active project for its working directory. Use /project or /unarchive first."
            )
        parent = self.frontend.project_channels.get(destination.channel_id)
        if parent is None:
            raise ValueError("The project channel is missing. Run setup_discord.py to repair it.")
        if source_message and source_message.channel.id != destination.channel_id:
            raise ValueError("The session directory belongs to a different project channel.")
        title = prompt_name(prompt) if prompt else info.get("name") or f"codex-{info['id'][:8]}"
        if source_message:
            await source_message.add_reaction("🚀")
            thread = await source_message.create_thread(
                name=thread_title(title), auto_archive_duration=10080
            )
        else:
            thread = await parent.create_thread(
                name=thread_title(title),
                type=discord.ChannelType.public_thread,
                auto_archive_duration=10080,
            )
        session = Session(
            thread.id,
            info["cwd"],
            title,
            info["id"],
            backend="app-server",
            native_settings=True,
            mirror_since=time.time(),
            display_model=result.get("model") or info.get("model") or "",
            source_message=source_message.id if source_message else None,
        )
        session.service_tier = self.store.meta.get("service_tier")
        self.store.sessions[thread.id] = session
        self.live.subscribed.add(info["id"])
        self.store.save()
        await self.live.call("thread/name/set", {"threadId": info["id"], "name": title})
        card = await self.say(
            thread, f"**{title}** · `{Path(session.cwd).name}`\nReply to talk · `!help`"
        )
        session.status_message, session.status_webhook = card.id, True
        self.store.save()
        if prompt:
            await self.send_prompt(thread, prompt)
            if source_message:
                await source_message.add_reaction("📡")
            else:
                await self.say(thread, f"-# 🧑 {prompt}")
        return thread

    async def send_prompt(self, thread, prompt):
        session = self.store.sessions[thread.id]
        if session.status == "ended":
            raise ValueError("This session ended. Use /revive or /resume first.")
        if thread.id in self.stopping:
            raise ValueError("This session is stopping; try again once it stops.")
        await self.ensure_live(session)
        result = "↪️" if await self.live.submit(session, prompt) == "steered" else "👀"
        session.status = "running"
        self.store.save()
        turn_id = getattr(self.live, "active_turns", {}).get(session.codex_thread)
        if turn_id:
            async with self.events.event_locks.setdefault(session.codex_thread, asyncio.Lock()):
                if turn_id not in getattr(self.live, "completed_turns", set()):
                    self.events.begin_activity(session, turn_id)
                    await self.events.update_live_status(session)
        if result == "👀":
            # Native turn settings persist in the daemon. Don't overwrite a later
            # change made from the user's terminal/editor on every Discord reply.
            session.service_tier = None
            session.collaboration_mode = None
            self.store.save()
        return result

    async def save_attachments(self, message):
        # Use the exact upstream upload naming, limits, and local-path convention.
        return await self.frontend.save_attachments(message)

    async def message(self, message):
        if message.author.id == self.frontend.user.id or message.webhook_id:
            return
        if not self.allowed(message.author.id, message.channel):
            return
        content = (message.content or "").strip()
        if content.startswith("!"):
            command, _, value = content[1:].partition(" ")
            args = text_arguments(command.lower(), value)
            return await self.controls.execute(
                args.pop("_command", command.lower()),
                message.channel,
                message.author,
                args,
                lambda text: self.say(message.channel, text),
            )
        attached = await self.save_attachments(message) if message.attachments else ""
        if attached:
            content = f"{content}\n{attached}".strip()
        if message.channel.id in self.store.sessions:
            if content:
                if message.author.id != self.owner:
                    content = f"{message.author.display_name}: {content}"
                reaction = await self.send_prompt(message.channel, content)
                await message.add_reaction(reaction)

    async def ensure_live(self, session):
        await self.live.connect()
        if not session.codex_thread:
            async with self.session_creation_lock:
                if not session.codex_thread:
                    result = await self.live.call(
                        "thread/start",
                        {
                            "cwd": session.cwd,
                            "approvalPolicy": "on-request",
                            "sandbox": self.options.sandbox,
                        },
                    )
                    session.codex_thread = result["thread"]["id"]
                    self.live.subscribed.add(session.codex_thread)
                    session.backend = "app-server"
                    self.store.save()
        try:
            await self.live.attach(session.codex_thread)
        except RpcError:
            restored = await asyncio.to_thread(codex_storage.restore, session.codex_thread)
            if not restored:
                await self.live.call("thread/unarchive", {"threadId": session.codex_thread})
            await self.live.attach(session.codex_thread)
        if session.backend == "exec":
            session.mirror_since = time.time()  # Its earlier exec replies were already posted.
        session.backend = "app-server"
        session.native_settings = True
        self.store.save()

    async def stop(self, thread, end=False):
        session = self.store.sessions[thread.id]
        if thread.id in self.stopping:
            raise ValueError("This session is already stopping.")
        self.stopping.add(thread.id)
        try:
            if end:
                return await self.end_session(thread, session)
            await self.ensure_live(session)
            await self.interrupt_session(session)
            session.status = "idle"
            self.store.save()
            await self.say(thread, "Stopped. Reply to continue.")
        finally:
            self.stopping.discard(thread.id)

    async def interrupt_session(self, session):
        await self.live.interrupt(session.codex_thread)
        deadline = time.monotonic() + 15
        while await self.live.active_turn(session.codex_thread):
            if time.monotonic() >= deadline:
                raise ValueError(
                    "Codex has not finished interrupting this turn yet. Try again shortly."
                )
            await asyncio.sleep(0.2)

    async def end_session(self, thread, session):
        # Closing Discord must not depend on loading damaged native history or
        # on Discord's much slower per-thread title-change rate limit.
        session.status = "ended"
        session.ended_seen_absent = False
        session.ended_at = time.time()
        session.deadline = None
        title = thread_title(session.name, ended=True)
        self.frontend._titles[thread.id] = title
        session.thread_title_cache = title
        self.store.save()
        await thread.edit(archived=True)
        self.frontend.retitle(thread, title)

        failures = []

        async def cleanup(label, operation):
            try:
                await asyncio.wait_for(operation(), timeout=20)
            except (RpcError, OSError, asyncio.TimeoutError, ValueError) as exc:
                LOG.warning("Could not %s for closed session %s: %s", label, session.codex_thread, exc)
                failures.append(label)

        await cleanup("close the terminal", lambda: self.terminal.close(session))
        if session.codex_thread:
            # Do not resume/unarchive a conversation merely to close it. The
            # existing actor can be interrupted/archived without attaching.
            await cleanup("interrupt Codex", lambda: self.interrupt_session(session))
            await cleanup(
                "archive Codex",
                lambda: self.live.call("thread/archive", {"threadId": session.codex_thread}),
            )
            self.live.subscribed.discard(session.codex_thread)
        self.store.save()
        if failures:
            return (
                "Discord thread closed. Could not "
                + "; ".join(failures)
                + ". The native conversation may still be running."
            )
        return "Session ended and Discord thread closed."

    async def maintenance(self):
        await self.wait_until_ready()
        boot = upstream.boot_id()
        previous = self.store.meta.get("boot_id")
        if upstream.REVIVE_ON_BOOT and previous and boot != previous:
            for session in list(self.store.sessions.values()):
                if session.status != "ended":
                    try:
                        await self.ensure_live(session)
                    except Exception:
                        LOG.exception("Could not revive Codex session %s", session.codex_thread)
        self.store.meta["boot_id"] = boot
        self.store.save()
        while not self.is_closed():
            try:
                for session in list(self.store.sessions.values()):
                    if (
                        session.status == "ended"
                        and session.ended_at
                        and time.time() - session.ended_at > upstream.ENDED_KEEP_DAYS * 86400
                    ):
                        self.store.sessions.pop(session.discord_thread, None)
                        self.store.save()
                        continue
                    if session.status != "ended" and session.delivery_failed:
                        await self.events.catch_up(session)
                    if upstream.REVIVE_ON_CRASH and session.status in {
                        "disconnected",
                        "interrupted",
                    }:
                        attempts = self.store.meta.setdefault("revive_attempts", {})
                        if time.time() - attempts.get(session.codex_thread, 0) > 60:
                            attempts[session.codex_thread] = time.time()
                            await self.ensure_live(session)
                    if session.status != "ended" and session.deadline:
                        left = session.deadline["until"] - time.time()
                        if 0 < left <= 120 and 120 not in session.deadline.setdefault("warned", []):
                            session.deadline["warned"].append(120)
                            await self.frontend.say(
                                await self.live_channel(session),
                                "🔴 Two minutes to the supernova.",
                                ping_owner=True,
                            )
                            self.store.save()
                    if (
                        session.status != "ended"
                        and session.deadline
                        and session.deadline["until"] <= time.time()
                    ):
                        action, message_id = (
                            session.deadline["action"],
                            session.deadline.get("message"),
                        )
                        session.deadline = None
                        self.store.save()
                        thread = await self.live_channel(session)
                        if message_id:
                            await thread.get_partial_message(message_id).edit(
                                content="💥 SUPERNOVA — time is up."
                            )
                        if action in {"stop", "kill"}:
                            await self.stop(thread, end=action == "kill")
                        if action != "kill":
                            await self.send_prompt(
                                thread,
                                "Supernova: the time budget is up. Wrap up, record what remains, and report here.",
                            )
            except Exception:
                LOG.exception("Codex maintenance failed")
            await asyncio.sleep(5)

    async def discover_once(self):
        known = set(self.store.sessions)
        subscribed = set(self.live.subscribed)
        await self.discovery.scan()
        for key in set(self.store.sessions) - known:
            if upstream.ANNOUNCE_NEW:
                thread = await self.live_channel(self.store.sessions[key])
                await self.frontend.say(
                    await self.parent_channel(thread),
                    f"🚀 **{self.store.sessions[key].name}** → <#{key}>",
                )
        for session in list(self.store.sessions.values()):
            if (
                session.status != "ended"
                and session.codex_thread in self.live.subscribed
                and session.codex_thread not in subscribed
            ):
                await self.events.catch_up(session)

    def get_channel(self, channel_id):
        return self.frontend.get_channel(channel_id)

    async def fetch_channel(self, channel_id):
        return await self.frontend.fetch_channel(channel_id)

    async def live_channel(self, session):
        channel = self.get_channel(session.discord_thread) or await self.fetch_channel(
            session.discord_thread
        )
        if channel.archived:
            await channel.edit(archived=False)
        return channel

    async def start_session(self, prompt, *, codex_id=None, source_message=None, cwd=None):
        # Two simultaneous /resume commands must not attach the same Codex ID twice.
        async with self.session_creation_lock:
            return await self._start_session(
                prompt, codex_id=codex_id, source_message=source_message, cwd=cwd
            )
