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


if __name__ == '__main__':
    unittest.main()
