import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import discord

from codex_backend import Session, SessionStore, TurnResult
from codex_bot import CodexBot, Config, NO_MENTIONS


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = SessionStore(root / 'state.json')
        self.store.sessions[20] = Session(20, str(root), 'test')
        config = Config('unused-test-token', 10, 1, {2}, root, self.store.path)
        self.runner = SimpleNamespace(run=AsyncMock(return_value=TurnResult('answer', 0, [], {})))
        self.bot = CodexBot(config, self.runner, self.store)
        self.thread = Mock(spec=discord.Thread)
        self.thread.id, self.thread.parent_id = 20, 10
        self.thread.send = AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))
        self.thread.edit = AsyncMock()
        self.thread.archived = False
        self.thread.mention = '<#20>'
        self.thread.send.return_value.id = 100
        self.thread.get_partial_message = Mock(return_value=SimpleNamespace(edit=AsyncMock()))
        self.webhook = SimpleNamespace(send=self.thread.send, edit_message=AsyncMock())
        self.bot.webhook_for = AsyncMock(return_value=self.webhook)

    async def asyncTearDown(self):
        await self.bot.close()
        self.tmp.cleanup()

    def test_all_commands_registered_without_claude(self):
        self.assertEqual({c.name for c in self.bot.tree.get_commands()},
                         {'codex', 'resume', 'sessions', 'stop', 'kill', 'model', 'effort', 'rename', 'help'})

    def test_access_checks_cover_owner_allowlist_outsiders_and_other_channels(self):
        self.assertTrue(self.bot.allowed(1, self.thread))
        self.assertTrue(self.bot.allowed(2, self.thread))
        self.assertFalse(self.bot.allowed(3, self.thread))
        self.thread.parent_id = 999
        self.assertFalse(self.bot.allowed(1, self.thread))
        self.assertFalse(self.bot.allowed(1, None))

    async def test_queue_serializes_turns(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def run(session, prompt, on_event):
            calls.append(prompt)
            if len(calls) == 1:
                entered.set()
                await release.wait()
            return TurnResult(prompt, 0, [], {})

        self.runner.run.side_effect = run
        self.assertFalse(self.bot.enqueue(self.thread, 'one'))
        await entered.wait()
        self.assertTrue(self.bot.enqueue(self.thread, 'two'))
        self.assertTrue(self.bot.enqueue(self.thread, 'three'))
        self.assertEqual(calls, ['one'])
        worker = self.bot.workers[20]
        release.set()
        await worker
        self.assertEqual(calls, ['one', 'two', 'three'])
        self.assertEqual(self.store.sessions[20].turns, 3)
        self.assertFalse(self.bot.workers)

    async def test_stop_discards_queue_and_session_can_continue(self):
        entered = asyncio.Event()

        async def run(*args):
            entered.set()
            await asyncio.Event().wait()

        self.runner.run.side_effect = run
        self.bot.enqueue(self.thread, 'one')
        await entered.wait()
        self.bot.enqueue(self.thread, 'two')
        await self.bot.stop(self.thread)
        self.assertEqual(self.store.sessions[20].status, 'idle')
        self.assertFalse(self.bot.workers)
        self.assertFalse(self.bot.queues)
        self.runner.run.side_effect = None
        self.bot.enqueue(self.thread, 'three')
        await self.bot.workers[20]
        self.assertEqual(self.runner.run.call_count, 2)

    async def test_failed_turn_does_not_replay_queue(self):
        self.runner.run.return_value = TurnResult('', 1, ['failure'], {})
        self.bot.enqueue(self.thread, 'one')
        self.bot.enqueue(self.thread, 'two')
        await self.bot.workers[20]
        self.assertEqual(self.runner.run.call_count, 1)
        self.assertEqual(self.store.sessions[20].status, 'error')

    async def test_stop_before_worker_first_runs_and_then_restart(self):
        self.bot.enqueue(self.thread, 'one')
        await self.bot.stop(self.thread)
        self.assertFalse(self.bot.queues)
        self.bot.enqueue(self.thread, 'two')
        await self.bot.workers[20]
        self.assertEqual(self.store.sessions[20].turns, 1)

    async def test_model_effort_and_mentions(self):
        await self.bot.control(self.thread, 'model', 'test-model')
        await self.bot.control(self.thread, 'effort', 'high')
        loaded = SessionStore(self.store.path).sessions[20]
        self.assertEqual((loaded.model, loaded.effort), ('test-model', 'high'))
        with self.assertRaises(ValueError):
            await self.bot.control(self.thread, 'model', '--inject')
        await self.bot.say(self.thread, '@everyone ' + 'a' * 4500)
        for call in self.thread.send.call_args_list:
            self.assertLessEqual(len(call.args[0]), 1900)
            self.assertIs(call.kwargs['allowed_mentions'], NO_MENTIONS)

    async def test_unauthorized_plain_messages_never_reach_runner(self):
        message = SimpleNamespace(author=SimpleNamespace(id=3, bot=False), channel=self.thread,
                                  content='do something', attachments=[])
        await self.bot.on_message(message)
        self.runner.run.assert_not_called()

    async def test_simultaneous_resume_attaches_exactly_one_thread(self):
        self.store.sessions.clear()
        self.bot.main_channel = SimpleNamespace(create_thread=AsyncMock(return_value=self.thread))
        self.bot.fetch_channel = AsyncMock(return_value=self.thread)
        session_id = '11111111-1111-4111-8111-111111111111'
        first, second = await asyncio.gather(
            self.bot.start_session('', codex_id=session_id),
            self.bot.start_session('', codex_id=session_id))
        self.assertIs(first, second)
        self.bot.main_channel.create_thread.assert_awaited_once()
        self.assertEqual(self.store.sessions[20].codex_thread, session_id)
        self.runner.run.assert_not_called()  # Attaching never replays a turn.

    async def test_new_thread_runs_prompt_and_persists_codex_id(self):
        self.store.sessions.clear()
        self.bot.main_channel = SimpleNamespace(create_thread=AsyncMock(return_value=self.thread))

        async def run(session, prompt, on_event):
            session.codex_thread = '11111111-1111-4111-8111-111111111111'
            await on_event({'type': 'thread.started', 'thread_id': session.codex_thread})
            return TurnResult('answer', 0, [], {})

        self.runner.run.side_effect = run
        await self.bot.start_session('hello')
        await self.bot.workers[20]
        loaded = SessionStore(self.store.path).sessions[20]
        self.assertEqual(loaded.codex_thread, '11111111-1111-4111-8111-111111111111')
        self.assertEqual(loaded.turns, 1)

    async def test_plain_main_channel_prompt_starts_session_without_mention(self):
        self.bot.start_session = AsyncMock(return_value=self.thread)
        channel = SimpleNamespace(id=10, send=AsyncMock())
        message = SimpleNamespace(author=SimpleNamespace(id=1, bot=False), channel=channel,
                                  content='Please explain the project', attachments=[], mentions=[])
        await self.bot.on_message(message)
        self.bot.start_session.assert_awaited_once_with('Please explain the project', project='', source_message=message)
        channel.send.assert_not_called()  # No detached-thread link in the parent channel.

    async def test_main_channel_ignores_bots_and_unauthorized_users(self):
        self.bot.start_session = AsyncMock()
        channel = SimpleNamespace(id=10, send=AsyncMock())
        for author in [SimpleNamespace(id=1, bot=True), SimpleNamespace(id=999, bot=False)]:
            await self.bot.on_message(SimpleNamespace(author=author, channel=channel, content='test'))
        self.bot.start_session.assert_not_called()

    async def test_prompt_creates_native_attached_thread_and_reactions_without_reposting_prompt(self):
        self.store.sessions.clear()
        channel = SimpleNamespace(id=10, send=AsyncMock())
        self.bot.main_channel = SimpleNamespace(create_thread=AsyncMock())
        message = SimpleNamespace(id=20, author=SimpleNamespace(id=1, bot=False), channel=channel,
                                  content='is your src on gh?', attachments=[], mentions=[],
                                  create_thread=AsyncMock(return_value=self.thread), add_reaction=AsyncMock())
        await self.bot.on_message(message)
        await asyncio.gather(*list(self.bot.workers.values()))
        message.create_thread.assert_awaited_once_with(name='🚀 is-your-src-on-gh', auto_archive_duration=10080)
        self.bot.main_channel.create_thread.assert_not_called()
        self.assertEqual([call.args[0] for call in message.add_reaction.call_args_list], ['🚀', '📡'])
        channel.send.assert_not_called()
        self.assertNotIn(message.content, [c.args[0] for c in self.thread.send.call_args_list])
        self.assertEqual(self.store.sessions[20].source_message, 20)
        self.runner.run.assert_awaited_once()
        self.assertEqual(self.runner.run.call_args.args[1], message.content)
        for call in self.webhook.send.call_args_list:
            self.assertIn(' · is-your-src-on-gh', call.kwargs['username'])
            self.assertIs(call.kwargs['thread'], self.thread)
            self.assertIn('seed=20', call.kwargs['avatar_url'])
            self.assertTrue(call.kwargs['wait'])

    async def test_message_launch_recognizes_leading_project_like_upstream(self):
        (self.bot.config.project_root / 'demo').mkdir()
        self.bot.start_session = AsyncMock()
        message = SimpleNamespace(channel=SimpleNamespace(id=10))
        await self.bot.launch_message(message, 'demo fix this bug')
        self.bot.start_session.assert_awaited_once_with('fix this bug', project='demo', source_message=message)

    async def test_attached_prompt_is_not_launched_twice(self):
        message = SimpleNamespace(id=20, create_thread=AsyncMock(), add_reaction=AsyncMock())
        self.bot.fetch_channel = AsyncMock(return_value=self.thread)
        result = await self.bot.start_session('hello', source_message=message)
        self.assertIs(result, self.thread)
        message.create_thread.assert_not_called()
        self.runner.run.assert_not_called()

    async def test_webhook_creation_is_serialized_and_reused(self):
        self.bot.main_channel = SimpleNamespace(webhooks=AsyncMock(return_value=[]),
                                               create_webhook=AsyncMock(return_value=self.webhook))
        first, second = await asyncio.gather(CodexBot.webhook_for(self.bot), CodexBot.webhook_for(self.bot))
        self.assertIs(first, self.webhook)
        self.assertIs(second, self.webhook)
        self.bot.main_channel.create_webhook.assert_awaited_once_with(name='chert-codex')

    async def test_legacy_live_status_is_upgraded_once_without_restarting_session(self):
        self.set_up_live()
        session = self.store.sessions[20]
        session.backend, session.codex_thread, session.status_message = 'app-server', 'external', 99
        await self.bot.discover_once()
        await self.bot.discover_once()
        self.assertTrue(session.status_webhook)
        self.thread.send.assert_awaited_once()
        self.bot.main_channel.create_thread.assert_not_called()
        self.runner.run.assert_not_called()

    def set_up_live(self):
        info = {'id': 'external', 'cwd': '/existing-project-outside-spawn-root', 'name': 'Existing session',
                'source': 'vscode', 'status': {'type': 'active'}}
        self.bot.live = SimpleNamespace(loaded_threads=AsyncMock(return_value=[info]),
                                       attach=AsyncMock(), close=AsyncMock(), submit=AsyncMock(return_value='steered'),
                                       interrupt=AsyncMock(), subscribed=set())
        self.bot.main_channel = SimpleNamespace(create_thread=AsyncMock(return_value=self.thread))
        self.bot.fetch_channel = AsyncMock(return_value=self.thread)
        return info

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
        self.runner.run.assert_not_called()

    async def test_live_replies_and_stop_control_original_session_without_exec(self):
        self.store.sessions.clear()
        self.set_up_live()
        await self.bot.discover_once()
        self.assertEqual(await self.bot.send_prompt(self.thread, 'follow-up'), '↪️')
        self.bot.live.submit.assert_awaited_once()
        self.runner.run.assert_not_called()
        await self.bot.stop(self.thread, end=True)
        self.bot.live.interrupt.assert_awaited_once_with('external')
        await self.bot.discover_once()
        self.assertEqual(self.store.sessions[20].status, 'ended')
        self.bot.main_channel.create_thread.assert_awaited_once()

    async def test_live_output_is_mirrored_once_and_reasoning_is_not_posted(self):
        self.store.sessions.clear()
        self.set_up_live()
        await self.bot.discover_once()
        self.thread.send.reset_mock()
        event = {'method': 'item/completed', 'params': {'threadId': 'external', 'turnId': 'turn1',
                 'item': {'id': 'item1', 'type': 'agentMessage', 'text': 'Hello @everyone'}}}
        await self.bot.handle_live_event(event)
        await self.bot.handle_live_event(event)
        event['params']['item'] = {'id': 'private', 'type': 'reasoning', 'text': 'Not public output'}
        await self.bot.handle_live_event(event)
        self.thread.send.assert_awaited_once()
        self.assertEqual(self.thread.send.call_args.args[0], 'Hello @everyone')
        self.assertIs(self.thread.send.call_args.kwargs['allowed_mentions'], NO_MENTIONS)
        self.bot.store = SessionStore(self.store.path)
        event['params']['item'] = {'id': 'item1', 'type': 'agentMessage', 'text': 'Hello @everyone'}
        await self.bot.handle_live_event(event)
        self.thread.send.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
