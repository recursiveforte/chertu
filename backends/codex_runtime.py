"""Native session discovery and lifecycle shared by Codex's Discord adapter.

This controller uses the frontend's gateway; it is not a second Discord client.
"""
import asyncio
import logging
from pathlib import Path

import discord

from backends.codex_state import Session
from backends.codex_live import LiveCodex, RpcError, discoverable, live_status
from backends.codex_presentation import activity_text, thread_title

LOG = logging.getLogger(__name__)


class CodexRuntime:
    def __init__(self, frontend, config, options, store):
        self.frontend, self.config, self.options, self.store = frontend, config, options, store
        self.main_channel = None
        self.owner = config.owner_id
        self.stopping = set()
        self.session_creation_lock = asyncio.Lock()
        self.live = LiveCodex(config.live_socket or Path.home() / '.codex/app-server-control/app-server-control.sock')
        self.background_tasks = []

    def get_channel(self, channel_id):
        return self.frontend.get_channel(channel_id)

    async def fetch_channel(self, channel_id):
        return await self.frontend.fetch_channel(channel_id)

    async def fetch_guild(self, guild_id):
        return await self.frontend.fetch_guild(guild_id)

    async def discover_loop(self):
        await self.wait_until_ready()
        warned = False
        while not self.is_closed():
            try:
                await self.live.connect()
                await self.discover_once()
                warned = False
            except (OSError, ConnectionError, RpcError, asyncio.TimeoutError) as exc:
                if not warned:
                    LOG.warning('Codex live discovery unavailable; will retry: %s', exc)
                    warned = True
            except Exception:
                LOG.exception('Codex discovery failed; will retry')
            await asyncio.sleep(self.config.discovery_interval)

    async def discover_once(self):
        threads = await self.live.loaded_threads()
        loaded_ids = {t['id'] for t in threads}
        for info in threads:
            if not discoverable(info):
                continue
            if info['id'] in self.store.meta.get('discovery_excluded', []):
                continue
            try:
                async with self.session_creation_lock:
                    session = next((s for s in self.store.sessions.values() if s.codex_thread == info['id']), None)
                    if session is not None and session.status == 'ended':
                        if not session.ended_seen_absent:
                            continue  # Ignore a stale in-flight snapshot just after /kill.
                        # An external client explicitly resumed this conversation after
                        # it disappeared. Reopen its original Discord thread, as upstream does.
                        await self.live_channel(session)
                        session.status = live_status(info)
                        session.ended_seen_absent = False
                        self.store.save()
                    if session is None:
                        parent = await self.discovery_channel(info)
                        if parent is None:
                            continue
                    await self.live.attach(info['id'])
                    if session is None:
                        title = info.get('name') or info.get('agentNickname') or Path(info['cwd']).name or 'Codex'
                        thread = await parent.create_thread(
                            name=thread_title(title), type=discord.ChannelType.public_thread,
                            auto_archive_duration=1440)
                        session = Session(thread.id, info['cwd'], title, info['id'],
                                          status=live_status(info), backend='app-server')
                        self.store.sessions[thread.id] = session
                        self.store.save()  # Record the mapping before subscribing to notifications.
                        card = await self.say(thread,
                            f'**Codex · {title}** · `{info["cwd"]}`\n'
                            f'**{session.status}** · Discovered an existing session. Reply here to talk to it; '
                            'your terminal and Discord share the same conversation.')
                        session.status_message = card.id
                        session.status_webhook = True
                        self.store.save()
                        LOG.info('Discovered Codex session %s → Discord thread %s', info['id'], thread.id)
                    elif session.backend != 'app-server':
                        session.backend = 'app-server'
                        self.store.save()
                    if info.get('model') and session.display_model != info['model']:
                        session.display_model = info['model']
                        self.store.save()
                    await self.observe_session(session, info)
                    if not session.status_webhook:
                        # Upgrade the old bot-authored status card once, without
                        # recreating the thread or replaying the conversation.
                        channel = await self.live_channel(session)
                        await channel.edit(name=thread_title(session.name))
                        card = await self.say(channel, activity_text(session, session.status, session.turn_started))
                        session.status_message, session.status_webhook = card.id, True
                        self.store.save()
                    await self.observe_status(session, info)
            except (RpcError, discord.HTTPException, OSError, asyncio.TimeoutError) as exc:
                LOG.warning('Could not discover Codex session %s: %s', info['id'], exc)
        for session in list(self.store.sessions.values()):
            if session.status == 'ended' and session.codex_thread not in loaded_ids and not session.ended_seen_absent:
                session.ended_seen_absent = True
                self.store.save()
            if session.backend == 'app-server' and session.status != 'ended' and session.codex_thread not in loaded_ids:
                if session.status != 'disconnected':
                    session.status = 'disconnected'
                    self.store.save()
                    await self.update_live_status(session)
                self.live.subscribed.discard(session.codex_thread)

    async def live_channel(self, session):
        channel = self.get_channel(session.discord_thread) or await self.fetch_channel(session.discord_thread)
        if channel.archived:
            await channel.edit(archived=False)
        return channel

    async def live_events(self):
        await self.wait_until_ready()
        while not self.is_closed():
            event = await self.live.notifications.get()
            try:
                await self.handle_live_event(event)
            except Exception:
                LOG.exception('Could not mirror Codex event to Discord')
            finally:
                self.live.notifications.task_done()

    async def start_session(self, prompt, project='', codex_id=None, source_message=None):
        # Two simultaneous /resume commands must not attach the same Codex ID twice.
        async with self.session_creation_lock:
            return await self._start_session(prompt, project, codex_id, source_message)

    def session_list(self):
        rows = [f'<#{s.discord_thread}> · **{s.status}** · {s.turns} turns'
                + (f' · `{s.codex_thread}`' if s.codex_thread else '')
                for s in self.store.sessions.values()]
        return '\n'.join(rows[-40:]) or 'No sessions yet. Type a prompt in the main channel to start.'
