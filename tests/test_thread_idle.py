import asyncio
from datetime import datetime, timezone
import json
import time
import unittest
from unittest.mock import patch

import discord
import test_codex_activity as activity_fixtures
from chert.backends.codex.backend import THREAD_INACTIVITY_SECONDS
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


class ThreadArchiveTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = ThreadIdleTests.asyncTearDown

    async def asyncSetUp(self):
        await ThreadIdleTests.asyncSetUp(self)
        self.session.last_interaction_at = self.now - THREAD_INACTIVITY_SECONDS
        self.channel.last_message_id = None
        self.channel.archive_timestamp = None

    async def archive(self):
        with patch('chert.backends.codex.backend.time.time', return_value=self.now):
            await self.adapter.archive_inactive_thread(self.session)

    async def test_archive_at_one_week_preserves_conversation_and_mapping(self):
        await self.archive()
        self.channel.edit.assert_awaited_once_with(
            archived=True, reason='Seven days of inactivity'
        )
        self.assertIs(self.adapter.store.sessions[300], self.session)
        self.assertEqual(self.session.status, 'idle')
        self.adapter.live.interrupt.assert_not_called()
        self.assertEqual([c.args[0] for c in self.adapter.live.call.call_args_list], ['thread/read'])

    async def test_just_under_one_week_stays_open(self):
        self.session.last_interaction_at += 1
        await self.archive()
        self.channel.edit.assert_not_called()
        self.adapter.fetch_channel.assert_not_called()

    async def test_recent_completion_or_discord_message_stays_open(self):
        for source in ('completion', 'message', 'reopen'):
            with self.subTest(source=source):
                self.session.last_completed_at = 0
                self.channel.last_message_id = None
                self.channel.archive_timestamp = None
                recent = datetime.fromtimestamp(self.now - 60, timezone.utc)
                if source == 'completion':
                    self.session.last_completed_at = recent.timestamp()
                elif source == 'message':
                    self.channel.last_message_id = discord.utils.time_snowflake(recent)
                else:
                    self.channel.archive_timestamp = recent
                await self.archive()
                self.channel.edit.assert_not_called()

    async def test_running_waiting_and_ended_sessions_stay_untouched(self):
        for status in ('running', 'waiting', 'ended'):
            self.session.status = status
            await self.archive()
        self.channel.edit.assert_not_called()

    async def test_active_turn_and_pending_delivery_stay_open(self):
        self.session.active_turn = 'active'
        await self.archive()
        self.session.active_turn = None
        self.session.delivery_failed = True
        await self.archive()
        self.channel.edit.assert_not_called()

    async def test_native_activity_wins_over_stale_local_state(self):
        for info in ({'status': {'type': 'active'}}, {'updatedAt': self.now - 60}):
            self.adapter.live.call.side_effect = None
            self.adapter.live.call.return_value = {'thread': info}
            await self.archive()
        self.channel.edit.assert_not_called()

    async def test_already_archived_thread_is_not_reopened(self):
        self.channel.archived = True
        await self.archive()
        self.channel.edit.assert_not_called()
        self.adapter.live.call.assert_not_called()

    async def test_native_failure_leaves_thread_open_for_retry(self):
        self.adapter.live.call.side_effect = OSError('offline')
        with self.assertRaises(OSError):
            await self.archive()
        self.channel.edit.assert_not_called()
        self.adapter.live.call.side_effect = None
        self.adapter.live.call.return_value = {'thread': {}}
        await self.archive()
        self.assertTrue(self.channel.archived)

    async def test_discord_failure_retries_without_ending_session(self):
        self.channel.edit.side_effect = OSError('offline')
        with self.assertRaises(OSError):
            await self.archive()
        self.assertEqual(self.session.status, 'idle')
        self.channel.edit.side_effect = None
        await self.archive()
        self.assertEqual(self.channel.edit.await_count, 2)

    async def test_new_activity_during_fetch_prevents_archive(self):
        async def fetch(channel_id):
            self.session.last_interaction_at = self.now
            return self.channel
        self.adapter.fetch_channel.side_effect = fetch
        await self.archive()
        self.channel.edit.assert_not_called()

    async def test_archive_clock_survives_restart(self):
        self.adapter.store.save()
        self.adapter.store = SessionStore(self.adapter.store.path)
        self.session = self.adapter.store.sessions[300]
        await self.archive()
        self.assertTrue(self.channel.archived)

    async def test_fresh_activity_can_reopen_archived_thread(self):
        await self.archive()
        await self.adapter.live_channel(self.session)
        self.assertFalse(self.channel.archived)
