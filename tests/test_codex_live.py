import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from aiohttp import web

from chert.backends.codex.state import Session
from chert.backends.codex.client import LiveCodex, RpcError, discoverable


class LiveTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="chert-", dir="/tmp")
        self.socket = Path(self.tmp.name) / "server.sock"
        self.calls = []
        self.active = None
        self.fail_steer = False
        self.server_socket = None
        self.hanging = asyncio.Event()
        app = web.Application()
        app.router.add_get("/", self.handle)
        self.server = web.AppRunner(app)
        await self.server.setup()
        await web.UnixSite(self.server, str(self.socket)).start()
        self.client = LiveCodex(self.socket, timeout=1)
        await self.client.connect()

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.cleanup()
        self.tmp.cleanup()

    async def handle(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.server_socket = ws
        async for message in ws:
            data = message.json()
            self.calls.append(data)
            method, params = data.get("method"), data.get("params", {})
            if "id" not in data:
                continue
            result = {}
            if method == "thread/loaded/list":
                result = {
                    "data": ["second"] if params.get("cursor") else ["first"],
                    "nextCursor": None if params.get("cursor") else "page2",
                }
            elif method == "thread/read":
                result = {
                    "thread": {
                        "id": params["threadId"],
                        "cwd": "/project",
                        "source": "cli",
                        "status": {"type": "active" if self.active else "idle"},
                    }
                }
            elif method == "thread/turns/list":
                result = {
                    "data": [{"id": self.active, "status": "inProgress"}] if self.active else []
                }
            elif method == "turn/steer" and self.fail_steer:
                await ws.send_json({"id": data["id"], "error": {"message": "active turn changed"}})
                continue
            elif method == "turn/start":
                result = {"turn": {"id": "new-turn", "status": "inProgress"}}
            elif method == "hang":
                self.hanging.set()
                continue
            elif method == "fail":
                await ws.send_json({"id": data["id"], "error": {"message": "test failure"}})
                continue
            await ws.send_json({"id": data["id"], "result": result})
        return ws

    async def test_lists_all_pages_and_attaches_without_overriding_configuration(self):
        threads = await self.client.loaded_threads()
        self.assertEqual([t["id"] for t in threads], ["first", "second"])
        await asyncio.gather(self.client.attach("first"), self.client.attach("first"))
        calls = [c for c in self.calls if c.get("method") == "thread/resume"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["params"], {"threadId": "first", "excludeTurns": True})

    async def test_progress_notifications_are_forwarded_but_raw_reasoning_is_not(self):
        for method in (
            "item/agentMessage/delta",
            "item/reasoning/summaryTextDelta",
            "turn/plan/updated",
        ):
            await self.server_socket.send_json({"method": method, "params": {"threadId": "first"}})
            event = await asyncio.wait_for(self.client.notifications.get(), 1)
            self.assertEqual(event["method"], method)
        await self.server_socket.send_json(
            {"method": "item/reasoning/textDelta", "params": {"delta": "private"}}
        )
        await self.client.call("ping", {})
        self.assertTrue(self.client.notifications.empty())

    async def test_idle_message_starts_turn_with_model_but_preserves_permissions(self):
        session = Session(1, "/project", "test", "first", model="example-model", effort="high")
        self.assertEqual(await self.client.submit(session, "hello"), "started")
        call = next(c for c in self.calls if c.get("method") == "turn/start")
        self.assertEqual(
            call["params"],
            {
                "threadId": "first",
                "input": [{"type": "text", "text": "hello"}],
                "model": "example-model",
                "effort": "high",
            },
        )

    async def test_promote_claims_exact_queue_entry_before_steering_with_native_input(self):
        queued = {
            "id": "q2",
            "clientUserMessageId": "chert:123",
            "input": [
                {"type": "text", "text": "follow-up"},
                {"type": "localImage", "path": "/tmp/picture.png"},
            ],
        }
        self.client.active_turn = AsyncMock(return_value="active")
        self.client.call = AsyncMock(
            side_effect=[
                {"data": [], "nextCursor": "second"},
                {"data": [queued]},
                {"deleted": True},
                {"turnId": "active"},
            ]
        )
        self.assertEqual(
            await self.client.promote_queued("first", "chert:123"),
            {
                "id": "active",
                "status": "inProgress",
            },
        )
        calls = self.client.call.await_args_list
        self.assertEqual(
            [c.args[0] for c in calls],
            [
                "thread/queue/list",
                "thread/queue/list",
                "thread/queue/delete",
                "turn/steer",
            ],
        )
        self.assertEqual(calls[2].args[1], {"threadId": "first", "queuedSubmissionId": "q2"})
        self.assertEqual(
            calls[3].args[1],
            {
                "threadId": "first",
                "clientUserMessageId": "chert:123",
                "expectedTurnId": "active",
                "input": queued["input"],
            },
        )

    async def test_promote_does_not_replay_missing_or_concurrently_consumed_entry(self):
        self.client.active_turn = AsyncMock(return_value="active")
        for pages in (
            [{"data": []}],
            [
                {"data": [{"id": "q", "clientUserMessageId": "client", "input": []}]},
                {"deleted": False},
            ],
        ):
            self.client.call = AsyncMock(side_effect=pages)
            self.assertIsNone(await self.client.promote_queued("first", "client"))
            self.assertNotIn("turn/steer", [c.args[0] for c in self.client.call.await_args_list])

    async def test_promote_idle_thread_uses_atomic_native_queue_start(self):
        self.client.active_turn = AsyncMock(return_value=None)
        self.client.call = AsyncMock(
            side_effect=[
                {"data": [{"id": "q", "clientUserMessageId": "client", "input": []}]},
                {"turn": {"id": "new", "status": "inProgress"}},
            ]
        )
        self.assertEqual((await self.client.promote_queued("first", "client"))["id"], "new")
        self.client.call.assert_awaited_with(
            "thread/queue/start",
            {
                "threadId": "first",
                "queuedSubmissionId": "q",
            },
        )

    async def test_rejected_promotion_returns_to_native_queue_but_timeout_never_replays(self):
        self.client.active_turn = AsyncMock(return_value="active")
        for failure in (RpcError("turn ended"), TimeoutError()):
            self.client.call = AsyncMock(
                side_effect=[
                    {"data": [{"id": "q", "clientUserMessageId": "client", "input": []}]},
                    {"deleted": True},
                    failure,
                    {},
                ]
            )
            with self.assertRaises(type(failure)):
                await self.client.promote_queued("first", "client")
            methods = [c.args[0] for c in self.client.call.await_args_list]
            self.assertEqual(methods.count("thread/queue/add"), int(isinstance(failure, RpcError)))
            self.assertEqual(methods.count("turn/steer"), 1)

    async def test_busy_message_steers_same_turn_without_second_writer(self):
        self.active = "turn-123"
        session = Session(1, "/project", "test", "first")
        self.assertEqual(await self.client.submit(session, "follow-up"), "steered")
        call = next(c for c in self.calls if c.get("method") == "turn/steer")
        self.assertEqual(call["params"]["expectedTurnId"], "turn-123")
        self.assertFalse(any(c.get("method") == "turn/start" for c in self.calls))
        await self.client.interrupt("first")
        self.assertEqual(self.calls[-1]["params"], {"threadId": "first", "turnId": "turn-123"})

    async def test_native_queue_owns_busy_and_idle_prompt_dispatch(self):
        session = Session(1, "/project", "test", "first", native_settings=True)
        self.client.attach = AsyncMock()
        for busy in (None, "turn-123"):
            with self.subTest(busy=busy):
                self.client.active_turn = AsyncMock(return_value=busy)

                async def call(method, params):
                    if method == "thread/queue/add":
                        return {"queuedSubmission": {"id": "queued-id"}}
                    if method == "thread/queue/list":
                        return {"data": [{"id": "queued-id"}] if busy else []}
                    self.fail(f"Unexpected native operation: {method}")

                self.client.call = AsyncMock(side_effect=call)
                result = await self.client.submit(
                    session, "later", queue=True, client_id="chert:123"
                )
                self.assertEqual(result, "queued" if busy else "started")
                self.client.call.assert_any_await(
                    "thread/queue/add",
                    {
                        "threadId": "first",
                        "input": [{"type": "text", "text": "later"}],
                        "clientUserMessageId": "chert:123",
                    },
                )

    async def test_native_queue_applies_settings_for_subsequent_turns(self):
        session = Session(
            1,
            "/project",
            "test",
            "first",
            native_settings=True,
            pending_settings={"model": "chosen", "config": {"model_reasoning_effort": "high"}},
        )
        self.client.attach = AsyncMock()
        self.client.active_turn = AsyncMock(return_value="busy")

        async def call(method, params):
            if method == "thread/queue/add":
                return {"queuedSubmission": {"id": "queued-id"}}
            if method == "thread/queue/list":
                return {"data": [{"id": "queued-id"}]}
            return {}

        self.client.call = AsyncMock(side_effect=call)
        await self.client.submit(session, "later", queue=True)
        self.client.call.assert_any_await(
            "thread/settings/update",
            {
                "threadId": "first",
                "model": "chosen",
                "effort": "high",
            },
        )
        self.assertEqual(session.pending_settings, {})

    async def test_accepted_queue_read_failure_does_not_resubmit_or_report_rejection(self):
        self.client.attach = AsyncMock()
        self.client.active_turn = AsyncMock(return_value="busy")
        self.client.call = AsyncMock(
            side_effect=[
                {"queuedSubmission": {"id": "queued-id"}},
                RpcError("temporary read failure"),
            ]
        )
        self.assertEqual(
            await self.client.submit(
                Session(1, "/project", "test", "first"),
                "later",
                queue=True,
            ),
            "queued",
        )
        self.assertEqual(self.client.call.await_count, 2)

    async def test_explicit_native_model_and_effort_are_applied_once_on_next_turn(self):
        session = Session(
            1,
            "/project",
            "test",
            "first",
            native_settings=True,
            pending_settings={
                "model": "chosen-model",
                "config": {"model_reasoning_effort": "high"},
            },
        )
        await self.client.submit(session, "hello")
        call = next(c for c in self.calls if c.get("method") == "turn/start")
        self.assertEqual(call["params"]["model"], "chosen-model")
        self.assertEqual(call["params"]["effort"], "high")
        self.assertEqual(session.pending_settings, {})
        self.assertEqual(session.display_model, "chosen-model")
        self.client.active_turns.clear()
        await self.client.submit(session, "next")
        calls = [c for c in self.calls if c.get("method") == "turn/start"]
        self.assertNotIn("model", calls[-1]["params"])
        self.assertNotIn("effort", calls[-1]["params"])

    async def test_model_selection_survives_steering_until_a_new_turn(self):
        self.active = "turn-123"
        session = Session(
            1,
            "/project",
            "test",
            "first",
            native_settings=True,
            pending_settings={"model": "chosen-model"},
        )
        await self.client.submit(session, "follow-up")
        self.assertEqual(session.pending_settings, {"model": "chosen-model"})

    async def test_rejected_turn_keeps_model_choice_for_retry(self):
        session = Session(
            1,
            "/project",
            "test",
            "first",
            native_settings=True,
            pending_settings={"model": "chosen-model"},
        )
        self.client.attach = AsyncMock()
        self.client.active_turn = AsyncMock(return_value=None)
        self.client.call = AsyncMock(side_effect=RpcError("server overloaded"))
        with self.assertRaises(RpcError):
            await self.client.submit(session, "hello")
        self.assertEqual(session.pending_settings, {"model": "chosen-model"})

    async def test_newer_model_choice_is_not_cleared_by_an_earlier_turn_response(self):
        session = Session(
            1,
            "/project",
            "test",
            "first",
            native_settings=True,
            pending_settings={"model": "first-choice"},
        )
        self.client.attach = AsyncMock()
        self.client.active_turn = AsyncMock(return_value=None)

        async def call(method, params):
            session.pending_settings["model"] = "newer-choice"
            return {"turn": {"id": "turn", "status": "inProgress"}}

        self.client.call = AsyncMock(side_effect=call)
        await self.client.submit(session, "hello")
        self.assertEqual(session.pending_settings, {"model": "newer-choice"})
        self.assertEqual(session.display_model, "first-choice")

    async def test_steer_race_is_reported_without_resending_prompt(self):
        self.active, self.fail_steer = "turn-123", True
        with self.assertRaises(RpcError):
            await self.client.submit(Session(1, "/project", "test", "first"), "follow-up")
        self.assertEqual(sum(c.get("method") == "turn/steer" for c in self.calls), 1)
        self.assertFalse(any(c.get("method") == "turn/start" for c in self.calls))

    async def test_immediate_followup_uses_accepted_turn_even_when_read_status_lags(self):
        session = Session(1, "/project", "test", "first")
        await self.client.submit(session, "first prompt")
        # Fake thread/read still says idle, just as a real daemon can immediately
        # after accepting turn/start. The returned turn ID is authoritative.
        self.assertIsNone(self.active)
        self.assertEqual(await self.client.submit(session, "follow-up"), "steered")
        self.assertEqual(sum(c.get("method") == "turn/start" for c in self.calls), 1)
        self.assertEqual(self.calls[-1]["params"]["expectedTurnId"], "new-turn")

    async def test_notifications_dont_block_rpc_and_do_not_answer_other_clients_approvals(self):
        await self.server_socket.send_json(
            {"method": "item/completed", "params": {"threadId": "first"}}
        )
        await self.server_socket.send_json(
            {
                "id": 1000,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "first"},
            }
        )
        first = await asyncio.wait_for(self.client.notifications.get(), 1)
        second = await asyncio.wait_for(self.client.notifications.get(), 1)
        self.assertEqual(first["method"], "item/completed")
        self.assertEqual(second["method"], "chert/inputRequired")
        await self.client.call("thread/read", {"threadId": "first"})
        self.assertFalse(any(c.get("id") == 1000 for c in self.calls))

    async def test_disconnect_fails_pending_rpc_and_reconnect_resubscribes(self):
        await self.client.attach("first")
        task = asyncio.create_task(self.client.call("hang", {}))
        await self.hanging.wait()
        await self.server_socket.close()
        with self.assertRaises(ConnectionError):
            await task
        await self.client.connect()
        await self.client.attach("first")
        self.assertEqual(sum(c.get("method") == "thread/resume" for c in self.calls), 2)

    async def test_rpc_errors_propagate(self):
        with self.assertRaisesRegex(RpcError, "test failure"):
            await self.client.call("fail", {})


class DiscoveryScopeTests(unittest.TestCase):
    def test_only_user_facing_loaded_sessions_are_discovered(self):
        root = {"id": "session", "cwd": "/some-project", "source": "vscode"}
        self.assertTrue(discoverable(root))
        self.assertFalse(discoverable({**root, "ephemeral": True}))
        self.assertFalse(discoverable({**root, "source": {"subAgent": {"parent": "session"}}}))
        self.assertFalse(discoverable({**root, "canAcceptDirectInput": False}))
