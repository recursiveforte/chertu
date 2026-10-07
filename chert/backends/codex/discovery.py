"""Discover native Codex sessions and attach them to registered projects."""

import asyncio
import logging
from pathlib import Path

import discord

from chert.backends.codex.state import Session
from chert.backends.codex.client import RpcError, discoverable, live_status
from chert.backends.codex.presentation import activity_text, thread_title

LOG = logging.getLogger(__name__)


class CodexDiscovery:
    def __init__(self, backend):
        self.backend = backend

    async def run(self):
        await self.backend.wait_until_ready()
        warned = False
        while not self.backend.is_closed():
            try:
                await self.backend.live.connect()
                await self.backend.discover_once()
                warned = False
            except (OSError, ConnectionError, RpcError, asyncio.TimeoutError) as exc:
                if not warned:
                    LOG.warning("Codex live discovery unavailable; will retry: %s", exc)
                    warned = True
            except Exception:
                LOG.exception("Codex discovery failed; will retry")
            await asyncio.sleep(self.backend.config.discovery_interval)

    async def scan(self):
        threads = await self.backend.live.loaded_threads()
        loaded_ids = {t["id"] for t in threads}
        for info in threads:
            if not discoverable(info):
                continue
            if info["id"] in self.backend.store.meta.get("discovery_excluded", []):
                continue
            try:
                async with self.backend.session_creation_lock:
                    session = next(
                        (
                            s
                            for s in self.backend.store.sessions.values()
                            if s.codex_thread == info["id"]
                        ),
                        None,
                    )
                    if session is not None and session.status == "ended":
                        if not session.ended_seen_absent:
                            continue  # Ignore a stale in-flight snapshot just after /kill.
                        # An external client explicitly resumed this conversation after
                        # it disappeared. Reopen its original Discord thread, as upstream does.
                        await self.backend.live_channel(session)
                        session.status = live_status(info)
                        session.ended_seen_absent = False
                        self.backend.store.save()
                    if session is None:
                        parent = await self.backend.discovery_channel(info)
                        if parent is None:
                            continue
                    await self.backend.live.attach(info["id"])
                    if session is None:
                        title = (
                            info.get("name")
                            or info.get("agentNickname")
                            or Path(info["cwd"]).name
                            or "Codex"
                        )
                        thread = await parent.create_thread(
                            name=thread_title(title),
                            type=discord.ChannelType.public_thread,
                            auto_archive_duration=1440,
                        )
                        session = Session(
                            thread.id,
                            info["cwd"],
                            title,
                            info["id"],
                            status=live_status(info),
                            backend="app-server",
                        )
                        self.backend.store.sessions[thread.id] = session
                        self.backend.store.save()  # Record the mapping before subscribing to notifications.
                        card = await self.backend.say(
                            thread,
                            f"**Codex · {title}** · `{info['cwd']}`\n"
                            f"**{session.status}** · Discovered an existing session. Reply here to talk to it; "
                            "your terminal and Discord share the same conversation.",
                        )
                        session.status_message = card.id
                        session.status_webhook = True
                        self.backend.store.save()
                        LOG.info(
                            "Discovered Codex session %s → Discord thread %s", info["id"], thread.id
                        )
                    elif session.backend != "app-server":
                        session.backend = "app-server"
                        self.backend.store.save()
                    if info.get("model") and session.display_model != info["model"]:
                        session.display_model = info["model"]
                        self.backend.store.save()
                    await self.backend.events.observe_session(session, info)
                    if not session.status_webhook:
                        # Upgrade the old bot-authored status card once, without
                        # recreating the thread or replaying the conversation.
                        channel = await self.backend.live_channel(session)
                        await channel.edit(name=thread_title(session.name))
                        card = await self.backend.say(
                            channel, activity_text(session, session.status, session.turn_started)
                        )
                        session.status_message, session.status_webhook = card.id, True
                        self.backend.store.save()
                    await self.backend.events.observe_status(session, info)
            except (RpcError, discord.HTTPException, OSError, asyncio.TimeoutError) as exc:
                LOG.warning("Could not discover Codex session %s: %s", info["id"], exc)
        for session in list(self.backend.store.sessions.values()):
            if (
                session.status == "ended"
                and session.codex_thread not in loaded_ids
                and not session.ended_seen_absent
            ):
                session.ended_seen_absent = True
                self.backend.store.save()
            if (
                session.backend == "app-server"
                and session.status != "ended"
                and session.codex_thread not in loaded_ids
            ):
                if session.status != "disconnected":
                    session.status = "disconnected"
                    self.backend.store.save()
                    await self.backend.events.update_live_status(session)
                self.backend.live.subscribed.discard(session.codex_thread)
