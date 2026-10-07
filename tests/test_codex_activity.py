import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

import test_backend_parity as fixtures
from chert.backends.codex.state import Session


class ActivityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.BackendParityTests.asyncSetUp
    asyncTearDown = fixtures.BackendParityTests.asyncTearDown

    async def event(self, method, **params):
        await self.adapter.events.handle_live_event(
            {"method": method, "params": {"threadId": self.session.codex_thread, **params}}
        )

    async def start(self, turn="one"):
        await self.event(
            "turn/started", turn={"id": turn, "status": "inProgress", "startedAt": time.time() - 30}
        )

    async def test_thread_emoji_tracks_lifecycle_without_rename_notices(self):
        self.host.say = AsyncMock()
        await self.start()
        await asyncio.sleep(0)
        self.channel.edit.assert_awaited_with(name="🔭 original")
        await self.event(
            "thread/status/changed",
            status={"type": "active", "activeFlags": ["waitingOnUserInput"]},
        )
        await asyncio.sleep(0)
        self.channel.edit.assert_awaited_with(name="📡 original")
        await self.event("thread/status/changed", status={"type": "active"})
        await asyncio.sleep(0)
        self.channel.edit.assert_awaited_with(name="🔭 original")
        await self.event("turn/completed", turn={"id": "one", "status": "completed"})
        await asyncio.sleep(0)
        self.channel.edit.assert_awaited_with(name="💤 original")
        self.host.say.assert_not_called()

    async def test_muting_messages_still_updates_thread_status(self):
        self.session.muted = True
        await self.start()
        await asyncio.sleep(0)
        self.channel.edit.assert_awaited_with(name="🔭 original")
        self.adapter.say.assert_not_called()

    async def test_rate_limited_rename_does_not_block_events_and_latest_title_wins(self):
        editing, release = asyncio.Event(), asyncio.Event()

        async def edit(**kwargs):
            editing.set()
            await release.wait()
            if "name" in kwargs:
                self.channel.name = kwargs["name"]

        self.channel.edit.side_effect = edit
        await self.start()
        await asyncio.wait_for(editing.wait(), 1)
        # The visible name can already match the final state while an old
        # working rename is in flight; it must still be superseded.
        self.channel.name = "💤 original"
        await asyncio.wait_for(
            self.event("turn/completed", turn={"id": "one", "status": "completed"}), 1
        )
        self.assertEqual(self.host._titles[300], "💤 original")
        release.set()
        await asyncio.gather(*self.host._title_tasks)
        self.assertEqual(self.channel.name, "💤 original")

    async def test_discovery_retries_failed_rename_with_cached_desired_title(self):
        self.channel.name = "🚀 original"
        self.channel.edit.side_effect = discord.HTTPException(
            SimpleNamespace(status=503, reason="Unavailable"), "Temporary outage"
        )
        await self.start()
        await asyncio.gather(*self.host._title_tasks)
        self.channel.edit.side_effect = None
        await self.adapter.events.observe_session(self.session, {})
        await asyncio.gather(*self.host._title_tasks)
        self.assertEqual(self.channel.edit.await_count, 2)
        self.channel.edit.assert_awaited_with(name="🔭 original")

    async def test_ending_session_supersedes_pending_working_rename(self):
        release = asyncio.Event()

        async def edit(**kwargs):
            if kwargs.get("name") == "🔭 original":
                await release.wait()
            if "name" in kwargs:
                self.channel.name = kwargs["name"]

        self.channel.edit.side_effect = edit
        await self.start()
        await asyncio.sleep(0)
        await asyncio.wait_for(self.adapter.stop(self.channel, end=True), 1)
        self.channel.edit.assert_any_await(archived=True)
        release.set()
        await asyncio.gather(*self.host._title_tasks)
        self.assertEqual(self.channel.name, "🌌 original")

    async def test_turn_start_renders_working_before_any_model_output(self):
        await self.start()
        self.assertIn("**exploring**", self.adapter.say.call_args.args[1])
        self.assertIn("0m 30s", self.adapter.say.call_args.args[1])
        self.assertEqual(self.session.status_message, 900)

    async def test_deleted_card_is_replaced_and_saved(self):
        await self.start()
        self.session.activity["last_edit"] = 0
        hook = await self.adapter.webhook_for()
        hook.edit_message.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "Unknown Message"
        )
        self.session.activity["body"] = ""
        await self.adapter.events.update_live_status(self.session)
        self.assertEqual(self.adapter.say.await_count, 2)
        self.assertEqual(self.session.status_message, 900)

    async def test_tool_burst_coalesces_then_flushes_without_waiting_for_another_event(self):
        await self.start()
        for method in ("item/started", "item/completed"):
            await self.event(
                method,
                turnId="one",
                item={"id": "cmd", "type": "commandExecution", "command": "pytest"},
            )
        self.assertEqual(self.session.activity["counts"], {"Bash": 1})
        self.assertTrue(self.session.activity["dirty"])
        self.session.activity["last_edit"] = 0
        await self.adapter.events.activity_tick()
        hook = await self.adapter.webhook_for()
        self.assertIn("$ pytest", hook.edit_message.call_args.kwargs["content"])
        self.assertFalse(self.session.activity["dirty"])

    async def test_public_progress_summaries_and_plan_reach_card(self):
        await self.start()
        await self.event(
            "item/reasoning/summaryTextDelta",
            turnId="one",
            itemId="summary",
            delta="Checking tests",
        )
        await self.event(
            "turn/plan/updated",
            turnId="one",
            plan=[{"step": "Run verification", "status": "inProgress"}],
        )
        self.session.activity["last_edit"] = 0
        await self.adapter.events.activity_tick()
        hook = await self.adapter.webhook_for()
        body = hook.edit_message.call_args.kwargs["content"]
        self.assertIn("Checking tests", body)
        self.assertIn("Run verification", body)

    async def test_completion_bypasses_throttle_and_freezes_elapsed(self):
        await self.start()
        await self.event("thread/status/changed", status={"type": "idle"})
        self.assertEqual(self.session.status, "running")
        await self.event(
            "turn/completed",
            turn={
                "id": "one",
                "status": "completed",
                "completedAt": self.session.turn_started + 32,
            },
        )
        hook = await self.adapter.webhook_for()
        body = hook.edit_message.call_args.kwargs["content"]
        self.assertIn("✅ turn done · 0m 32s", body)
        with patch("chert.backends.codex.presentation.time.time", return_value=time.time() + 500):
            await self.adapter.events.update_live_status(self.session)
        self.assertEqual(hook.edit_message.call_args.kwargs["content"], body)
        self.assertEqual(self.session.turns, 1)

    async def test_new_turn_gets_new_card_without_reusing_done_card(self):
        await self.start()
        await self.event("turn/completed", turn={"id": "one", "status": "completed"})
        await self.start("two")
        await self.start("two")
        self.assertEqual(self.adapter.say.await_count, 2)
        self.assertEqual(self.session.active_turn, "two")

    async def test_waiting_and_interruption_are_never_reported_as_done(self):
        await self.start()
        await self.event(
            "thread/status/changed", status={"type": "active", "activeFlags": ["waitingOnApproval"]}
        )
        hook = await self.adapter.webhook_for()
        self.assertIn("📡 **signal**", hook.edit_message.call_args.kwargs["content"])
        await self.event("turn/completed", turn={"id": "one", "status": "interrupted"})
        body = hook.edit_message.call_args.kwargs["content"]
        self.assertIn("⏹ stopped", body)
        self.assertNotIn("turn done", body)

    async def test_attach_mid_turn_restores_working_card_and_original_start(self):
        self.adapter.live.call.side_effect = None
        self.adapter.live.call.return_value = {
            "data": [{"id": "one", "status": "inProgress", "startedAt": time.time() - 125}]
        }
        await self.adapter.events.observe_status(self.session, {"status": {"type": "active"}})
        self.assertIn("**exploring** · 2m 05s", self.adapter.say.call_args.args[1])
        self.assertEqual(self.session.active_turn, "one")

    async def test_missed_completion_repairs_card_and_schedules_reply_recovery(self):
        await self.start()
        self.adapter.live.call.side_effect = None
        self.adapter.live.call.return_value = {
            "data": [{"id": "one", "status": "completed", "completedAt": time.time()}]
        }
        await self.adapter.events.observe_status(self.session, {"status": {"type": "idle"}})
        hook = await self.adapter.webhook_for()
        self.assertIn("turn done", hook.edit_message.call_args.kwargs["content"])
        self.assertTrue(self.session.delivery_failed)
        self.assertEqual(self.session.mirror_since, 0)
        self.assertEqual(self.session.mirrored_turns, [])

    async def test_one_failed_card_does_not_block_other_heartbeats(self):
        await self.start()
        other = Session(400, "/project", "other", "other", backend="app-server", status="running")
        self.adapter.store.sessions[400] = other
        self.session.activity["last_edit"] = 0

        async def update(session):
            if session is self.session:
                raise ConnectionError("Discord unavailable")

        self.adapter.events.update_live_status = AsyncMock(side_effect=update)
        with self.assertLogs("chert.backends.codex.events", level="ERROR"):
            await self.adapter.events.activity_tick()
        self.adapter.events.update_live_status.assert_any_await(other)

    async def test_transport_disconnect_cannot_leave_a_false_working_indicator(self):
        await self.start()
        await self.adapter.events.handle_live_event({"method": "chert/disconnected", "params": {}})
        hook = await self.adapter.webhook_for()
        self.assertIn("**disconnected**", hook.edit_message.call_args.kwargs["content"])
        self.assertNotIn("turn done", hook.edit_message.call_args.kwargs["content"])
        self.assertTrue(self.session.delivery_failed)

    async def test_failed_completion_edit_is_retried_even_though_turn_is_idle(self):
        await self.start()
        hook = await self.adapter.webhook_for()
        hook.edit_message.side_effect = ConnectionError("temporary outage")
        with self.assertRaises(ConnectionError):
            await self.event("turn/completed", turn={"id": "one", "status": "completed"})
        hook.edit_message.side_effect = None
        self.session.activity["last_edit"] = 0
        await self.adapter.events.activity_tick()
        self.assertIn("turn done", hook.edit_message.call_args.kwargs["content"])
        self.assertFalse(self.session.activity["dirty"])

    async def test_subagent_progress_uses_upstream_aggregation_and_renderer(self):
        await self.start()
        self.host.render_subs = AsyncMock()
        self.adapter.events.update_subagents(
            self.session, {"a": {"status": "running"}, "b": {"status": "pendingInit"}}
        )
        self.adapter.events.update_subagents(
            self.session, {"a": {"status": "completed"}, "b": {"status": "running"}}
        )
        self.adapter.events.update_subagents(self.session, {"a": {"status": "shutdown"}})
        self.assertEqual(self.session.subagents["live"], {"b": "Codex"})
        self.assertEqual(self.session.subagents["done"], {"Codex": 1})
        await self.adapter.events.activity_tick()
        self.host.render_subs.assert_awaited_once()
        self.assertFalse(self.session.subagents["dirty"])
        self.adapter.main_channel.create_thread.assert_not_called()

    async def test_compaction_notices_are_visible_and_not_duplicated(self):
        await self.start()
        self.adapter.say.reset_mock()
        for method in ("item/started", "item/completed", "item/completed"):
            await self.event(
                method, turnId="one", item={"id": "compact", "type": "contextCompaction"}
            )
        self.assertEqual(self.adapter.say.await_count, 2)
        self.assertIn("compacting context", self.adapter.say.call_args_list[0].args[1])
        self.assertIn("compacted", self.adapter.say.call_args_list[1].args[1])

    async def test_retry_is_visible_without_marking_the_turn_done(self):
        await self.start()
        await self.event("error", willRetry=True, error={"message": "Temporarily unavailable"})
        await self.event("error", willRetry=True, error={"message": "Temporarily unavailable"})
        self.assertEqual(self.session.status, "running")
        notices = [c.args[1] for c in self.adapter.say.call_args_list if "retrying" in c.args[1]]
        self.assertEqual(len(notices), 1)
        self.session.activity["last_edit"] = 0
        await self.adapter.events.activity_tick()
        hook = await self.adapter.webhook_for()
        self.assertIn("Retrying:", hook.edit_message.call_args.kwargs["content"])
