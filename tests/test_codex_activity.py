import asyncio
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

import test_backend_parity as fixtures
from chert.backends.codex.state import Session
from chert.backends.codex.client import LiveCodex


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

    async def test_viewing_an_image_does_not_upload_it_to_discord(self):
        self.host.deliver_session_file = AsyncMock()
        await self.start()
        for path in ("/uploads/user-image.png", "/project/generated-image.png"):
            with self.subTest(path=path):
                for method in ("item/started", "item/completed"):
                    await self.event(
                        method,
                        turnId="one",
                        item={"id": path, "type": "imageView", "path": path},
                    )
                self.host.deliver_session_file.assert_not_called()
        await self.event("turn/completed", turn={"id": "one", "status": "completed"})
        self.assertEqual(self.session.status, "idle")

    async def test_explicit_image_delivery_still_uploads_to_the_session(self):
        path = Path(self.tmp.name) / "image.png"
        path.write_bytes(b"image fixture")
        await self.host.deliver_session_file(
            str(path), "Requested image", None, self.session.codex_thread, self.session.cwd
        )
        self.channel.send.assert_awaited_once()
        sent = self.channel.send.call_args
        self.assertEqual(sent.args, ("Requested image",))
        self.assertEqual(sent.kwargs["file"].filename, "image.png")
        self.assertEqual(sent.kwargs["file"].fp.read(), b"image fixture")
        sent.kwargs["file"].close()

    async def test_activity_changes_do_not_rename_the_thread(self):
        self.channel.name = "🚀 original"
        self.session.thread_title_cache = self.channel.name
        await self.start()
        await self.event(
            "thread/status/changed",
            status={"type": "active", "activeFlags": ["waitingOnUserInput"]},
        )
        await self.event("thread/status/changed", status={"type": "active"})
        await self.event("turn/completed", turn={"id": "one", "status": "completed"})
        await self.adapter.events.handle_live_event({"method": "chert/disconnected", "params": {}})
        await self.adapter.events.observe_session(self.session, {})
        await asyncio.sleep(0)
        self.channel.edit.assert_not_called()
        self.assertEqual(self.session.thread_title_cache, "🚀 original")
        self.assertTrue(self.adapter.say.called)

    async def test_discovery_retries_failed_rename_with_cached_desired_title(self):
        self.channel.name = "💤 original"
        self.channel.edit.side_effect = discord.HTTPException(
            SimpleNamespace(status=503, reason="Unavailable"), "Temporary outage"
        )
        await self.adapter.events.observe_session(self.session, {})
        await asyncio.gather(*self.host._title_tasks)
        self.channel.edit.side_effect = None
        await self.adapter.events.observe_session(self.session, {})
        await asyncio.gather(*self.host._title_tasks)
        self.assertEqual(self.channel.edit.await_count, 2)
        self.channel.edit.assert_awaited_with(name="🚀 original")

    async def test_ending_session_supersedes_pending_name_rename(self):
        release = asyncio.Event()

        async def edit(**kwargs):
            if kwargs.get("name") == "🚀 original":
                await release.wait()
            if "name" in kwargs:
                self.channel.name = kwargs["name"]

        self.channel.edit.side_effect = edit
        await self.adapter.events.observe_session(self.session, {})
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

    async def test_closed_thread_stays_archived_across_activity_ticks_and_restart(self):
        from chert.backends.codex.state import SessionStore

        await self.start()
        self.session.activity["last_edit"] = 0
        self.session.subagents["dirty"] = True

        async def edit(**kwargs):
            if "archived" in kwargs:
                self.channel.archived = kwargs["archived"]

        self.channel.edit.side_effect = edit
        await self.adapter.stop(self.channel, end=True)
        await asyncio.gather(*self.host._title_tasks)
        self.channel.edit.reset_mock()
        self.adapter.say.reset_mock()
        hook = await self.adapter.webhook_for()
        hook.edit_message.reset_mock()
        self.host.render_subs = AsyncMock()
        for restart in (False, True):
            if restart:
                self.adapter.store = SessionStore(self.adapter.store.path)
            await self.adapter.events.activity_tick()
            await self.adapter.events.activity_tick()
            await self.adapter.events.update_live_status(self.adapter.store.sessions[300])
            self.assertTrue(self.channel.archived)
        self.channel.edit.assert_not_called()
        self.adapter.say.assert_not_called()
        hook.edit_message.assert_not_called()
        self.host.render_subs.assert_not_called()

    async def test_activity_queued_before_close_rechecks_status_after_lock(self):
        await self.start()
        self.session.activity["last_edit"] = 0
        self.session.subagents["dirty"] = True
        self.host.render_subs = AsyncMock()
        lock = self.adapter.events.event_locks[self.session.codex_thread]
        async with lock:
            tick = asyncio.create_task(self.adapter.events.activity_tick())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.session.status = "ended"
            self.channel.archived = True
        await tick
        self.assertFalse(self.channel.edit.called)
        self.host.render_subs.assert_not_called()

    async def test_close_drains_in_flight_status_update_before_archiving(self):
        await self.start()
        entered, release = asyncio.Event(), asyncio.Event()
        hook = await self.adapter.webhook_for()

        async def edit_message(*args, **kwargs):
            entered.set()
            await release.wait()

        hook.edit_message.side_effect = edit_message
        event = asyncio.create_task(self.event(
            "thread/status/changed",
            status={"type": "active", "activeFlags": ["waitingOnUserInput"]},
        ))
        await entered.wait()
        closing = asyncio.create_task(self.adapter.stop(self.channel, end=True))
        try:
            await asyncio.sleep(0)
            self.channel.edit.assert_not_called()
        finally:
            release.set()
            await asyncio.gather(event, closing)
        self.channel.edit.assert_any_await(archived=True)
        self.assertEqual(self.session.status, "ended")

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

    async def test_next_active_status_does_not_reopen_previous_completed_card(self):
        await self.start()
        await self.event("turn/completed", turn={"id": "one", "status": "completed"})
        hook = await self.adapter.webhook_for()
        hook.edit_message.reset_mock()
        await self.event("thread/status/changed", status={"type": "active"})
        hook.edit_message.assert_not_called()
        self.assertEqual(self.session.status, "idle")
        await self.start("two")
        self.assertEqual(self.adapter.say.await_count, 2)

    async def test_active_status_before_first_turn_does_not_create_extra_card(self):
        await self.event("thread/status/changed", status={"type": "active"})
        self.adapter.say.assert_not_called()
        await self.start()
        self.adapter.say.assert_awaited_once()

    async def test_submission_does_not_skip_completion_events_waiting_for_discord(self):
        await self.start()
        # The socket reader is already on turn two; Discord still has turn one's
        # completion in its backlog when another prompt is submitted.
        self.adapter.live.active_turns = {self.session.codex_thread: "two"}
        await self.adapter.send_prompt(self.channel, "third prompt")
        self.assertEqual(self.session.active_turn, "one")
        self.adapter.say.assert_awaited_once()
        await self.event("turn/completed", turn={"id": "one", "status": "completed"})
        hook = await self.adapter.webhook_for()
        self.assertIn("turn done", hook.edit_message.call_args.kwargs["content"])
        await self.start("two")
        self.assertEqual(self.adapter.say.await_count, 2)

    async def test_queued_submission_keeps_completed_card_final_until_turn_starts(self):
        await self.start()
        await self.event("turn/completed", turn={"id": "one", "status": "completed"})
        await self.adapter.send_prompt(self.channel, "next prompt")
        self.assertEqual(self.session.status, "idle")
        self.assertIsNone(self.session.active_turn)
        await self.adapter.events.activity_tick()
        hook = await self.adapter.webhook_for()
        self.assertIn("turn done", hook.edit_message.call_args.kwargs["content"])

    async def test_late_progress_for_completed_turn_cannot_create_another_card(self):
        await self.start()
        await self.event("turn/completed", turn={"id": "one", "status": "completed"})
        await self.start("two")
        await self.event("item/started", turnId="one", item={"id": "old", "type": "agentMessage"})
        await self.event("item/agentMessage/delta", turnId="one", itemId="old", delta="Old reply")
        self.assertEqual(self.session.active_turn, "two")
        self.assertEqual(self.adapter.say.await_count, 2)

    async def test_discovery_waits_for_events_even_after_consumer_takes_them(self):
        await self.start()
        live = LiveCodex(Path(self.tmp.name) / "unused.sock")
        live.call = AsyncMock(return_value={"data": [{"id": "two", "status": "inProgress"}]})
        self.adapter.live = live
        event = {"method": "turn/completed", "params": {
            "threadId": self.session.codex_thread, "turn": {"id": "one", "status": "completed"},
        }}
        live.queue_notification(event)
        taken = live.notifications.get_nowait()
        self.assertTrue(live.notifications.empty())
        await self.adapter.events.observe_status(self.session, {"status": {"type": "active"}})
        live.call.assert_not_called()
        self.assertEqual(self.session.active_turn, "one")
        await self.adapter.events.handle_live_event(taken)
        live.notifications.task_done()
        self.assertFalse(live.has_pending_notifications(self.session.codex_thread))
        await self.adapter.events.observe_status(self.session, {"status": {"type": "active"}})
        self.assertEqual(self.session.active_turn, "two")

    async def test_event_arriving_during_history_read_takes_precedence(self):
        await self.start()
        live = LiveCodex(Path(self.tmp.name) / "unused.sock")
        self.adapter.live = live

        async def read(*args):
            live.queue_notification({"method": "turn/completed", "params": {
                "threadId": self.session.codex_thread,
                "turn": {"id": "one", "status": "completed"},
            }})
            return {"data": [{"id": "two", "status": "inProgress"}]}

        live.call = AsyncMock(side_effect=read)
        await self.adapter.events.observe_status(self.session, {"status": {"type": "active"}})
        self.assertEqual(self.session.active_turn, "one")

    async def test_catchup_defers_without_losing_retry_when_events_are_pending(self):
        live = LiveCodex(Path(self.tmp.name) / "unused.sock")
        self.adapter.live = live
        live.call = AsyncMock()
        live.queue_notification({"method": "thread/status/changed", "params": {
            "threadId": self.session.codex_thread, "status": {"type": "active"},
        }})
        await self.adapter.events.catch_up(self.session)
        live.call.assert_not_called()
        self.assertTrue(self.session.delivery_failed)

    async def test_catchup_does_not_advance_the_card_to_a_newer_running_turn(self):
        await self.start()
        self.adapter.live.call.side_effect = None
        self.adapter.live.call.return_value = {"data": [{"id": "two", "status": "inProgress"}]}
        await self.adapter.events.catch_up(self.session)
        self.assertEqual(self.session.active_turn, "one")

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
