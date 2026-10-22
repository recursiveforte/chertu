import asyncio
import json
import time
import unittest
from unittest.mock import patch

import test_codex_activity as activity_fixtures
from chert.backends.codex.state import SessionStore
OLD_IDLE_SECONDS = 36 * 60 * 60


class ThreadIdleTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = activity_fixtures.ActivityTests.asyncTearDown
    event = activity_fixtures.ActivityTests.event
    start = activity_fixtures.ActivityTests.start

    async def asyncSetUp(self):
        await activity_fixtures.ActivityTests.asyncSetUp(self)
        self.channel.name = '🚀 original'
        self.session.thread_title_cache = self.channel.name
        self.now = time.time()
        self.session.last_interaction_at = self.now - OLD_IDLE_SECONDS

        async def edit(**kwargs):
            for key, value in kwargs.items():
                setattr(self.channel, key, value)
        self.channel.edit.side_effect = edit

    async def title_at(self, now):
        with patch('chert.backends.codex.events.time.time', return_value=now):
            await self.adapter.events.update_thread_title(self.session)
        await asyncio.gather(*self.host._title_tasks)

    async def test_inactivity_and_polling_keep_live_title(self):
        await self.title_at(self.now - 1)
        self.channel.edit.assert_not_called()
        await self.title_at(self.now)
        self.assertEqual(self.channel.name, '🚀 original')
        with patch('chert.backends.codex.events.time.time', return_value=self.now + 60):
            await self.adapter.events.observe_session(self.session, {})
        self.assertEqual(self.session.last_interaction_at, self.now - OLD_IDLE_SECONDS)
        self.channel.edit.assert_not_called()

    async def test_prompt_restores_legacy_sleeping_title(self):
        self.channel.name = '💤 original'
        await self.adapter.send_prompt(self.channel, 'hello')
        await asyncio.gather(*self.host._title_tasks)
        self.assertEqual(self.channel.name, '🚀 original')
        self.assertGreaterEqual(self.session.last_interaction_at, self.now)

    async def test_native_turn_restores_legacy_sleeping_title(self):
        self.channel.name = '💤 original'
        await self.start()
        await asyncio.gather(*self.host._title_tasks)
        self.assertEqual(self.channel.name, '🚀 original')

    async def test_native_snapshot_uses_activity_time_not_poll_time(self):
        with patch('chert.backends.codex.events.time.time', return_value=self.now):
            await self.adapter.events.observe_session(
                self.session, {'updatedAt': self.now - 3600}
            )
        self.assertEqual(self.session.last_interaction_at, self.now - 3600)
        self.assertEqual(self.channel.name, '🚀 original')

    async def test_running_and_ended_threads_do_not_sleep(self):
        for status in ('running', 'ended'):
            self.session.status = status
            await self.title_at(self.now)
        self.channel.edit.assert_not_called()

    async def test_old_inactivity_timestamp_stays_live_after_restart(self):
        self.adapter.store.save()
        self.adapter.store = SessionStore(self.adapter.store.path)
        self.session = self.adapter.store.sessions[300]
        await self.title_at(self.now)
        self.assertEqual(self.channel.name, '🚀 original')

    async def test_old_state_uses_recorded_activity(self):
        self.session.last_completed_at = self.now - OLD_IDLE_SECONDS
        self.adapter.store.save()
        data = json.loads(self.adapter.store.path.read_text())
        del data['sessions'][0]['last_interaction_at']
        self.adapter.store.path.write_text(json.dumps(data))
        self.adapter.store = SessionStore(self.adapter.store.path)
        self.session = self.adapter.store.sessions[300]
        await self.title_at(self.now)
        self.assertEqual(self.channel.name, '🚀 original')

    async def test_legacy_title_restored_without_unarchiving(self):
        self.channel.name = '💤 original'
        self.session.thread_title_cache = self.channel.name
        self.channel.archived = True
        await self.adapter.events.observe_session(self.session, {})
        await asyncio.gather(*self.host._title_tasks)
        self.channel.edit.assert_awaited_once_with(name='🚀 original')
        self.assertTrue(self.channel.archived)

    async def test_thread_command_restores_legacy_sleeping_title(self):
        self.channel.name = '💤 original'
        await self.adapter.controls.execute('help', self.channel, self.user, {}, self.respond)
        await asyncio.gather(*self.host._title_tasks)
        self.assertEqual(self.channel.name, '🚀 original')
