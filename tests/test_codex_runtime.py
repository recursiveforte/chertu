"""Exercise session lifecycle through the production adapter and shared gateway."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import discord

from backends.codex_state import Session, SessionStore
from config import Config, CodexOptions
from backends.codex_live import RpcError
from shared_frontend import SharedFrontend


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = SessionStore(root / 'state.json')
        self.store.sessions[20] = Session(20, str(root), 'test', 'external', backend='app-server')
        config = Config('unused-test-token', 10, 1, {2}, root, self.store.path)
        self.frontend = SharedFrontend(config, CodexOptions(), self.store, 0)
        self.frontend.owner = 1
        self.frontend.say = AsyncMock()
        self.frontend.retitle = Mock()
        self.frontend._connection.user = SimpleNamespace(id=999)
        self.bot = self.frontend.codex
        self.thread = Mock(spec=discord.Thread)
        self.thread.id, self.thread.parent_id = 20, 10
        self.thread.edit = AsyncMock()
        self.thread.archived = False
        self.thread.mention = '<#20>'
        self.bot.say = AsyncMock(return_value=SimpleNamespace(id=100))
        self.bot.update_live_status = AsyncMock()
        self.bot.terminal.close = AsyncMock()
        self.bot.main_channel = SimpleNamespace(id=10, create_thread=AsyncMock(return_value=self.thread))
        self.bot.fetch_channel = AsyncMock(return_value=self.thread)
        self.set_up_live()

    async def asyncTearDown(self):
        await self.frontend.close()
        self.tmp.cleanup()

    def set_up_live(self):
        info = {'id': 'external', 'cwd': str(self.bot.config.project_root), 'name': 'Existing session',
                'source': 'vscode', 'status': {'type': 'active'}}
        async def call(method, params):
            if method in {'thread/resume', 'thread/start'}:
                return {'thread': {**info, 'id': params.get('threadId', 'external')}}
            return {'data': []}
        self.bot.live = SimpleNamespace(loaded_threads=AsyncMock(return_value=[info]),
            connect=AsyncMock(), call=AsyncMock(side_effect=call), attach=AsyncMock(), close=AsyncMock(),
            submit=AsyncMock(return_value='steered'), interrupt=AsyncMock(), active_turn=AsyncMock(return_value=None),
            active_turns={}, subscribed=set())
        return info

    async def test_legacy_exec_session_is_adopted_without_replaying_a_turn(self):
        session = self.store.sessions[20]
        session.backend = 'exec'
        await self.bot.ensure_live(session)
        self.assertEqual(session.backend, 'app-server')
        self.assertTrue(session.native_settings)
        self.bot.live.attach.assert_awaited_once_with('external')
        self.bot.live.submit.assert_not_called()

    async def test_stop_rejects_concurrent_messages_and_interrupts_exactly_once(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def interrupt(sid):
            entered.set()
            await release.wait()
        self.bot.live.interrupt.side_effect = interrupt
        stopping = asyncio.create_task(self.bot.stop(self.thread))
        await entered.wait()
        with self.assertRaises(ValueError):
            await self.bot.send_prompt(self.thread, 'must not run')
        with self.assertRaises(ValueError):
            await self.bot.stop(self.thread)
        release.set()
        await stopping
        self.bot.live.interrupt.assert_awaited_once_with('external')
        self.bot.live.submit.assert_not_called()
        self.assertFalse(self.bot.stopping)
        self.assertEqual(self.store.sessions[20].status, 'idle')

    async def test_failed_submit_is_not_retried(self):
        self.bot.live.submit.side_effect = RpcError('connection lost')
        with self.assertRaises(RpcError):
            await self.bot.send_prompt(self.thread, 'change files')
        self.bot.live.submit.assert_awaited_once()

    async def test_access_checks_cover_owner_allowlist_and_unknown_channels(self):
        self.assertTrue(self.bot.allowed(1, self.thread))
        self.assertTrue(self.bot.allowed(2, self.thread))
        self.assertFalse(self.bot.allowed(3, self.thread))
        self.assertFalse(self.bot.allowed(1, SimpleNamespace(id=99)))
        self.assertFalse(self.bot.allowed(1, None))

    async def test_simultaneous_resume_attaches_exactly_one_thread(self):
        self.store.sessions.clear()
        self.bot.main_channel.create_thread = AsyncMock(return_value=self.thread)
        self.bot.fetch_channel = AsyncMock(return_value=self.thread)
        session_id = '11111111-1111-4111-8111-111111111111'
        first, second = await asyncio.gather(
            self.bot.start_session('', codex_id=session_id),
            self.bot.start_session('', codex_id=session_id))
        self.assertIs(first, second)
        self.bot.main_channel.create_thread.assert_awaited_once()
        self.assertEqual(self.store.sessions[20].codex_thread, session_id)

    async def test_attached_prompt_is_not_launched_twice(self):
        message = SimpleNamespace(id=20, create_thread=AsyncMock(), add_reaction=AsyncMock())
        self.bot.fetch_channel = AsyncMock(return_value=self.thread)
        result = await self.bot.start_session('hello', source_message=message)
        self.assertIs(result, self.thread)
        message.create_thread.assert_not_called()

    async def test_discovery_creates_one_thread_and_persists_mapping_across_restart(self):
        self.store.sessions.clear()
        self.set_up_live()
        await self.bot.discover_once()
        await self.bot.discover_once()
        self.bot.main_channel.create_thread.assert_awaited_once()
        saved = SessionStore(self.store.path).sessions[20]
        self.assertEqual(saved.codex_thread, 'external')
        self.assertEqual(saved.backend, 'app-server')
        self.bot.store = SessionStore(self.store.path)
        await self.bot.discover_once()
        self.bot.main_channel.create_thread.assert_awaited_once()

    async def test_unmaterialized_editor_tab_does_not_create_discord_thread_or_block_discovery(self):
        self.store.sessions.clear()
        info = self.set_up_live()
        self.bot.live.loaded_threads.return_value = [{**info, 'id': 'empty-tab'}, info]
        async def attach(sid):
            if sid == 'empty-tab':
                raise RpcError('no rollout found')
        self.bot.live.attach.side_effect = attach
        with self.assertLogs('backends.codex_runtime', level='WARNING'):
            await self.bot.discover_once()
        self.bot.main_channel.create_thread.assert_awaited_once()
        self.assertEqual(self.store.sessions[20].codex_thread, 'external')

    async def test_removed_test_session_is_not_rediscovered(self):
        self.store.sessions.clear()
        self.set_up_live()
        self.store.meta['discovery_excluded'] = ['external']
        await self.bot.discover_once()
        self.bot.main_channel.create_thread.assert_not_called()
        self.bot.live.attach.assert_not_called()

    async def test_live_replies_and_stop_control_original_session_without_exec(self):
        self.store.sessions.clear()
        self.set_up_live()
        await self.bot.discover_once()
        self.assertEqual(await self.bot.send_prompt(self.thread, 'follow-up'), '↪️')
        self.bot.live.submit.assert_awaited_once()
        await self.bot.stop(self.thread, end=True)
        self.bot.live.interrupt.assert_awaited_once_with('external')
        await self.bot.discover_once()
        self.assertEqual(self.store.sessions[20].status, 'ended')
        self.bot.main_channel.create_thread.assert_awaited_once()

    async def test_external_resume_reopens_existing_thread_after_observed_exit(self):
        self.store.sessions.clear()
        info = self.set_up_live()
        await self.bot.discover_once()
        self.store.sessions[20].status = 'ended'
        self.bot.live.loaded_threads.return_value = []
        await self.bot.discover_once()
        self.assertTrue(self.store.sessions[20].ended_seen_absent)
        self.bot.live.loaded_threads.return_value = [info]
        await self.bot.discover_once()
        self.assertEqual(self.store.sessions[20].status, 'running')
        self.bot.main_channel.create_thread.assert_awaited_once()

    async def test_live_output_is_mirrored_once_and_reasoning_is_not_posted(self):
        self.store.sessions.clear()
        self.set_up_live()
        await self.bot.discover_once()
        self.bot.say.reset_mock()
        event = {'method': 'item/completed', 'params': {'threadId': 'external', 'turnId': 'turn1',
                 'item': {'id': 'item1', 'type': 'agentMessage', 'text': 'Hello @everyone'}}}
        await self.bot.handle_live_event(event)
        await self.bot.handle_live_event(event)
        event['params']['item'] = {'id': 'private', 'type': 'reasoning', 'text': 'Not public output'}
        await self.bot.handle_live_event(event)
        self.bot.say.assert_awaited_once()
        self.assertEqual(self.bot.say.call_args.args[1], 'Hello @everyone')
        self.bot.store = SessionStore(self.store.path)
        event['params']['item'] = {'id': 'item1', 'type': 'agentMessage', 'text': 'Hello @everyone'}
        await self.bot.handle_live_event(event)
        self.bot.say.assert_awaited_once()
