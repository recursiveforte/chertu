"""Reproduce overlapping Discord launches without sending production messages."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import test_projects as fixtures
from test_audio import attachment


class ProjectBurstTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ProjectFrontendTests.asyncSetUp
    asyncTearDown = fixtures.ProjectFrontendTests.asyncTearDown
    interaction = fixtures.ProjectFrontendTests.interaction

    def message(self, ident, content=None, channel=100, user=7):
        parent = self.channels[channel]
        thread = SimpleNamespace(
            id=ident, parent_id=channel, parent=parent, archived=False,
            mention=f"<#{ident}>", send=AsyncMock(),
        )
        return SimpleNamespace(
            id=ident, channel=parent, content=content or f"prompt {ident}",
            author=SimpleNamespace(id=user, display_name=str(user)), webhook_id=None,
            attachments=[], create_thread=AsyncMock(return_value=thread),
            add_reaction=AsyncMock(), remove_reaction=AsyncMock(),
        )

    def native(self):
        count = 0

        async def call(method, params):
            nonlocal count
            if method == "thread/start":
                count += 1
                return {"thread": {"id": f"native-{count}", "cwd": params["cwd"]}}
            return {"data": []}

        self.bot.codex.live = SimpleNamespace(
            connect=AsyncMock(), call=AsyncMock(side_effect=call), subscribed=set(),
            attach=AsyncMock(), close=AsyncMock(), submit=AsyncMock(return_value="queued"),
            active_turns={},
        )
        self.bot.codex.say = AsyncMock(return_value=SimpleNamespace(id=900))
        self.bot.save_attachments = AsyncMock(return_value="")
        return self.bot.codex.live

    async def test_overlapping_launches_queue_in_one_native_and_discord_thread(self):
        live = self.native()
        entered, release = asyncio.Event(), asyncio.Event()
        first, second, third = [self.message(i) for i in (901, 902, 903)]
        thread = first.create_thread.return_value

        async def create(**kwargs):
            entered.set()
            await release.wait()
            return thread

        first.create_thread.side_effect = create
        launch = asyncio.create_task(self.bot.on_message(first))
        await entered.wait()
        # Even a slow launch that exceeds the grace window must retain its claim.
        self.bot.project_prompt_bursts.bursts[(100, 7)].last_received -= 6
        followups = [asyncio.create_task(self.bot.on_message(m)) for m in (second, third)]
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(launch, *followups), 5)
        self.assertEqual(len(self.bot.codex.store.sessions), 1)
        first.create_thread.assert_awaited_once()
        second.create_thread.assert_not_called()
        third.create_thread.assert_not_called()
        starts = [c for c in live.call.await_args_list if c.args[0] == "thread/start"]
        self.assertEqual(len(starts), 1)
        self.assertEqual([c.args[1] for c in live.submit.await_args_list],
                         [m.content for m in (first, second, third)])
        self.assertEqual({c.args[0].codex_thread for c in live.submit.await_args_list},
                         {"native-1"})
        self.assertEqual([c.kwargs["client_id"] for c in live.submit.await_args_list],
                         ["chert:901", "chert:902", "chert:903"])
        session = self.bot.codex.store.sessions[901]
        self.assertEqual([e["channel"] for e in session.prompt_messages], [100, 100, 100])
        for message in (first, second, third):
            message.add_reaction.assert_awaited_with("↪️")

    async def test_followup_after_creation_reuses_thread_but_later_prompt_starts_new(self):
        live = self.native()
        messages = [self.message(i) for i in (901, 902, 903)]
        await self.bot.on_message(messages[0])
        await self.bot.on_message(messages[1])
        messages[1].create_thread.assert_not_called()
        self.bot.project_prompt_bursts.bursts[(100, 7)].last_received -= 6
        await self.bot.on_message(messages[2])
        messages[2].create_thread.assert_awaited_once()
        self.assertEqual([c.args[0].codex_thread for c in live.submit.await_args_list],
                         ["native-1", "native-1", "native-2"])

    async def test_users_and_projects_have_independent_destinations(self):
        self.native()
        self.bot.config.allowed_users.add(8)
        self.projects.projects["two"].harness = "codex"
        messages = [self.message(901), self.message(902, user=8),
                    self.message(903, channel=200)]
        await asyncio.gather(*(self.bot.on_message(m) for m in messages))
        self.assertEqual(len(self.bot.codex.store.sessions), 3)
        for message in messages:
            message.create_thread.assert_awaited_once()

    async def test_explicit_text_and_slash_launches_bypass_burst(self):
        self.native()
        first, explicit, later = (self.message(901), self.message(902, "!codex separate"),
                                  self.message(903))
        for message in (first, explicit, later):
            await self.bot.on_message(message)
            message.create_thread.assert_awaited_once()
        self.bot.codex.launch = AsyncMock()
        await self.bot.route_command("codex", None, self.interaction(), {"prompt": "new"})
        self.bot.codex.launch.assert_awaited_once()
        self.assertNotIn((100, 7), self.bot.project_prompt_bursts.bursts)

    async def test_slow_audio_is_claimed_before_transcription_and_keeps_attachments(self):
        live = self.native()
        entered, release = asyncio.Event(), asyncio.Event()
        first, second = self.message(901), self.message(902)
        first.attachments = [attachment()]
        picture = attachment("diagram.png")
        second.attachments = [picture]

        async def transcribe(clip):
            entered.set()
            await release.wait()
            return "spoken context"

        self.bot.transcriber.transcribe = AsyncMock(side_effect=transcribe)
        self.bot.save_attachments.side_effect = lambda m: "[image path]" if picture in m.attachments else ""
        launch = asyncio.create_task(self.bot.on_message(first))
        await entered.wait()
        followup = asyncio.create_task(self.bot.on_message(second))
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(launch, followup), 5)
        self.assertIn("spoken context", live.submit.await_args_list[0].args[1])
        self.assertIn("[image path]", live.submit.await_args_list[1].args[1])
        second.create_thread.assert_not_called()
        self.assertEqual(len(self.bot.codex.store.sessions), 1)

    async def test_failed_launch_does_not_fan_out_waiting_messages_or_poison_retry(self):
        live = self.native()
        entered, release = asyncio.Event(), asyncio.Event()

        async def fail(**kwargs):
            entered.set()
            await release.wait()
            raise RuntimeError("Discord unavailable")

        first, second, retry = [self.message(i) for i in (901, 902, 903)]
        first.create_thread.side_effect = fail
        launch = asyncio.create_task(self.bot.on_message(first))
        await entered.wait()
        followup = asyncio.create_task(self.bot.on_message(second))
        await asyncio.sleep(0)
        release.set()
        with self.assertLogs("chert.discord.frontend", level="ERROR"):
            await asyncio.wait_for(asyncio.gather(launch, followup), 5)
        second.create_thread.assert_not_called()
        live.submit.assert_not_called()
        self.assertNotIn((100, 7), self.bot.project_prompt_bursts.bursts)
        await self.bot.on_message(retry)
        retry.create_thread.assert_awaited_once()
        live.submit.assert_awaited_once()

    async def test_existing_thread_messages_keep_their_destination(self):
        live = self.native()
        first, second = self.message(901), self.message(902)
        await self.bot.on_message(first)
        second.channel = first.create_thread.return_value
        await self.bot.on_message(second)
        self.assertEqual(len(self.bot.codex.store.sessions), 1)
        self.assertEqual([c.args[0].codex_thread for c in live.submit.await_args_list],
                         ["native-1", "native-1"])
