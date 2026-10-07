import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
import unittest

import discord

from chert.backends.codex.reactions import PromptReactions
from chert.backends.codex.state import SessionStore
from chert.backends.codex.client import RpcError
import test_codex_lifecycle as fixtures


class PromptReactionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.LifecycleTests.asyncSetUp
    asyncTearDown = fixtures.LifecycleTests.asyncTearDown
    set_up_live = fixtures.LifecycleTests.set_up_live

    def message(self, ident=123):
        return SimpleNamespace(
            id=ident, channel=self.thread, add_reaction=AsyncMock(), remove_reaction=AsyncMock()
        )

    async def event(self, method, **params):
        await self.bot.events.handle_live_event(
            {
                "method": method,
                "params": {"threadId": "external", **params},
            }
        )

    async def started(self, message, turn="two"):
        await self.event("turn/started", turn={"id": turn, "status": "inProgress"})
        await self.event(
            "item/started",
            turnId=turn,
            item={
                "type": "userMessage",
                "id": "input",
                "clientId": f"chert:{message.id}",
            },
        )

    def click(self, message=123, user=1, emoji="↪️", channel=20, guild=1, bot=False):
        return SimpleNamespace(
            message_id=message,
            user_id=user,
            channel_id=channel,
            guild_id=guild,
            emoji=discord.PartialEmoji(name=emoji),
            member=SimpleNamespace(bot=bot),
        )

    async def queued_click_setup(self):
        message = self.message()
        await self.bot.send_prompt(self.thread, "later", source=message)
        self.bot.get_channel = lambda _: self.thread
        self.bot.live.promote_queued = AsyncMock(
            return_value={"id": "active", "status": "inProgress"}
        )
        return message

    async def test_click_promotes_and_consumes_user_arrow_then_completes_with_active_turn(self):
        message = await self.queued_click_setup()
        await self.frontend.on_raw_reaction_add(self.click())
        self.bot.live.promote_queued.assert_awaited_once_with("external", "chert:123")
        message.add_reaction.assert_awaited_with("👀")
        self.assertTrue(any(c.args[1].id == 1 for c in message.remove_reaction.await_args_list))
        await self.event("turn/completed", turn={"id": "active", "status": "completed"})
        message.add_reaction.assert_awaited_with("✅")

    async def test_click_ignores_unauthorized_bots_wrong_emoji_guild_and_message(self):
        await self.queued_click_setup()
        for payload in (
            self.click(user=3),
            self.click(user=999),
            self.click(bot=True),
            self.click(emoji="👀"),
            self.click(guild=2),
            self.click(message=456),
            self.click(channel=999),
        ):
            await self.frontend.on_raw_reaction_add(payload)
        self.bot.live.promote_queued.assert_not_awaited()
        await self.frontend.on_raw_reaction_add(self.click(user=2, emoji="↪"))
        self.bot.live.promote_queued.assert_awaited_once()

    async def test_concurrent_clicks_do_not_promote_twice(self):
        await self.queued_click_setup()
        await asyncio.gather(*(self.frontend.on_raw_reaction_add(self.click()) for _ in range(2)))
        self.bot.live.promote_queued.assert_awaited_once()

    async def test_click_on_closed_stopping_or_archived_project_does_nothing(self):
        await self.queued_click_setup()
        session = self.store.sessions[20]
        session.status = "ended"
        await self.frontend.on_raw_reaction_add(self.click())
        session.status = "running"
        self.bot.stopping.add(20)
        await self.frontend.on_raw_reaction_add(self.click())
        self.bot.stopping.clear()
        self.frontend.projects.for_channel(self.thread).archived = True
        await self.frontend.on_raw_reaction_add(self.click())
        self.bot.live.promote_queued.assert_not_awaited()

    async def test_click_after_restart_works_without_cached_discord_message(self):
        message = await self.queued_click_setup()
        self.bot.store = SessionStore(self.store.path)
        self.bot.reactions = PromptReactions(self.bot)
        self.thread.get_partial_message.return_value = message
        await self.frontend.on_raw_reaction_add(self.click())
        self.bot.live.promote_queued.assert_awaited_once()
        message.add_reaction.assert_awaited_with("👀")

    async def test_failed_promotion_keeps_queue_status_and_reports_uncertainty(self):
        message = await self.queued_click_setup()
        self.bot.live.promote_queued.side_effect = TimeoutError()
        with self.assertLogs("chert.backends.codex.reactions", level="WARNING"):
            await self.frontend.on_raw_reaction_add(self.click())
        message.add_reaction.assert_awaited_once_with("↪️")
        self.assertIn("could not be confirmed", self.bot.say.await_args.args[1])

    async def test_promoted_prompt_reconciles_by_turn_id_without_native_client_id(self):
        message = await self.queued_click_setup()
        await self.frontend.on_raw_reaction_add(self.click())
        self.bot.live.call.side_effect = [
            {"data": []},
            {"data": [{"id": "active", "status": "completed", "items": []}]},
        ]
        await self.bot.reactions.reconcile(self.store.sessions[20])
        message.add_reaction.assert_awaited_with("✅")

    async def test_native_queue_running_and_completion_replace_only_bot_reactions(self):
        message = self.message()
        await self.bot.send_prompt(self.thread, "later", source=message)
        message.add_reaction.assert_awaited_with("↪️")
        self.bot.live.submit.assert_awaited_once_with(
            self.store.sessions[20],
            "later",
            queue=True,
            client_id="chert:123",
        )
        await self.started(message)
        message.add_reaction.assert_awaited_with("👀")
        await self.event("turn/completed", turn={"id": "two", "status": "completed"})
        self.assertEqual(
            [c.args[0] for c in message.add_reaction.await_args_list], ["↪️", "👀", "✅"]
        )
        self.assertTrue(
            all(c.args[1] is self.frontend.user for c in message.remove_reaction.await_args_list)
        )
        self.assertFalse(self.store.sessions[20].prompt_messages)

    async def test_idle_prompt_and_project_source_have_same_lifecycle(self):
        self.bot.live.submit.return_value = "started"
        source = self.message()
        source.channel = SimpleNamespace(id=10)
        await self.bot.send_prompt(self.thread, "hello", source=source)
        source.add_reaction.assert_awaited_with("👀")
        await self.started(source)
        await self.event("turn/completed", turn={"id": "two", "status": "completed"})
        self.assertEqual([c.args[0] for c in source.add_reaction.await_args_list], ["👀", "✅"])

    async def test_other_turn_completion_does_not_complete_queued_prompt(self):
        message = self.message()
        await self.bot.send_prompt(self.thread, "later", source=message)
        await self.event("turn/completed", turn={"id": "unrelated", "status": "completed"})
        message.add_reaction.assert_awaited_once_with("↪️")

    async def test_failed_and_interrupted_turns_are_not_marked_successful(self):
        for status in ("failed", "interrupted"):
            with self.subTest(status=status):
                message = self.message()
                await self.bot.send_prompt(self.thread, "later", source=message)
                await self.started(message, status)
                await self.event("turn/completed", turn={"id": status, "status": status})
                message.add_reaction.assert_awaited_with("❌")
                self.assertNotIn("✅", [c.args[0] for c in message.add_reaction.await_args_list])

    async def test_restart_recovers_completed_prompt_from_client_id_without_resubmitting(self):
        message = self.message()
        await self.bot.send_prompt(self.thread, "later", source=message)
        self.bot.store = SessionStore(self.store.path)
        self.bot.reactions = PromptReactions(self.bot)
        self.thread.get_partial_message.return_value = message
        self.bot.get_channel = lambda _: self.thread
        self.bot.live.call.side_effect = None
        self.bot.live.call.return_value = {
            "data": [
                {
                    "id": "two",
                    "status": "completed",
                    "startedAt": time.time(),
                    "items": [{"type": "userMessage", "clientId": "chert:123"}],
                }
            ]
        }

        async def call(method, params):
            return (
                {"data": []} if method == "thread/queue/list" else self.bot.live.call.return_value
            )

        self.bot.live.call.side_effect = call
        await self.bot.reactions.reconcile(self.bot.store.sessions[20])
        message.add_reaction.assert_awaited_with("✅")
        self.bot.live.submit.assert_awaited_once()
        self.assertFalse(self.bot.store.sessions[20].prompt_messages)

    async def test_reconciliation_keeps_native_queued_prompt_and_no_content_in_state(self):
        message = self.message()
        await self.bot.send_prompt(self.thread, "private queued content", source=message)
        self.bot.live.call.side_effect = None
        self.bot.live.call.return_value = {"data": [{"clientUserMessageId": "chert:123"}]}
        await self.bot.reactions.reconcile(self.store.sessions[20])
        message.add_reaction.assert_awaited_once_with("↪️")
        self.assertNotIn("private queued content", self.store.path.read_text())

    async def test_reaction_failure_retries_without_blocking_or_replaying_prompt(self):
        message = self.message()
        message.add_reaction.side_effect = discord.HTTPException(
            SimpleNamespace(status=503, reason="unavailable"),
            "try later",
        )
        with self.assertLogs("chert.backends.codex.reactions", level="WARNING"):
            await self.bot.send_prompt(self.thread, "later", source=message)
        message.add_reaction.side_effect = None
        await self.bot.reactions.sync(self.store.sessions[20])
        message.add_reaction.assert_awaited_with("↪️")
        self.bot.live.submit.assert_awaited_once()

    async def test_deleted_message_does_not_block_lifecycle(self):
        message = self.message()
        message.add_reaction.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason="missing"),
            "missing",
        )
        await self.bot.send_prompt(self.thread, "later", source=message)
        self.assertFalse(self.store.sessions[20].prompt_messages)

    async def test_removed_native_queue_entry_gets_failure_after_history_settles(self):
        message = self.message()
        await self.bot.send_prompt(self.thread, "later", source=message)
        session = self.store.sessions[20]
        session.prompt_messages[0]["created"] -= 61
        self.bot.live.call.side_effect = None
        self.bot.live.call.return_value = {"data": []}
        await self.bot.reactions.reconcile(session)
        message.add_reaction.assert_awaited_with("❌")
        self.bot.live.submit.assert_awaited_once()

    async def test_native_queue_reconciliation_follows_pagination(self):
        message = self.message()
        await self.bot.send_prompt(self.thread, "later", source=message)
        session = self.store.sessions[20]
        session.prompt_messages[0]["created"] -= 61
        self.bot.live.call.side_effect = [
            {"data": [{"clientUserMessageId": "another-client"}], "nextCursor": "page2"},
            {"data": [{"clientUserMessageId": "chert:123"}]},
        ]
        await self.bot.reactions.reconcile(session)
        message.add_reaction.assert_awaited_once_with("↪️")
        self.assertEqual(self.bot.live.call.await_count, 2)

    async def test_rpc_rejection_marks_failure_and_transport_error_is_not_replayed(self):
        message = self.message()
        self.bot.live.submit.side_effect = RpcError("rejected")
        with self.assertRaises(RpcError):
            await self.bot.send_prompt(self.thread, "later", source=message)
        message.add_reaction.assert_awaited_with("❌")
        self.bot.live.submit.side_effect = ConnectionError("lost acknowledgement")
        with self.assertRaises(ConnectionError):
            await self.bot.send_prompt(self.thread, "later", source=self.message(124))
        self.assertEqual(self.store.sessions[20].prompt_messages[0]["client_id"], "chert:124")

    async def test_completion_waits_for_submission_mapping(self):
        message = self.message()

        async def submit(*args, **kwargs):
            self.pending = asyncio.create_task(self.started(message))
            await asyncio.sleep(0)
            return "queued"

        self.bot.live.submit.side_effect = submit
        await self.bot.send_prompt(self.thread, "later", source=message)
        await self.pending
        await self.event("turn/completed", turn={"id": "two", "status": "completed"})
        self.assertEqual(
            [c.args[0] for c in message.add_reaction.await_args_list], ["↪️", "👀", "✅"]
        )
