import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import test_backend_parity as fixtures
from chert.discord.prompts import FormModal, request_view


class ElicitationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.BackendParityTests.asyncSetUp
    asyncTearDown = fixtures.BackendParityTests.asyncTearDown

    def request(self, schema, mode="form", key="connection:1"):
        request = {
            "id": key,
            "method": "mcpServer/elicitation/request",
            "params": {
                "threadId": self.session.codex_thread,
                "mode": mode,
                "message": 'Allow Computer Use to use "Messages"?',
                "requestedSchema": schema,
            },
        }
        self.adapter.live.server_requests[key] = request
        return request_view(self.adapter, key, request)

    def interaction(self, user=None):
        return SimpleNamespace(
            user=user or self.user,
            channel=self.channel,
            response=SimpleNamespace(
                defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock()
            ),
            message=SimpleNamespace(edit=AsyncMock()),
        )

    async def test_empty_object_permission_requires_explicit_allow_click(self):
        for mode in ("form", "openai/form", "openaiForm"):
            with self.subTest(mode=mode):
                self.adapter.live.answer.reset_mock()
                body, view = self.request({"type": "object", "properties": {}}, mode)
                self.assertIn('Allow Computer Use to use "Messages"?', body)
                self.assertEqual([b.label for b in view.children], ["Allow", "Decline", "Cancel"])
                self.adapter.live.answer.assert_not_called()
                interaction = self.interaction()
                self.assertTrue(await view.interaction_check(interaction))
                await view.children[0].callback(interaction)
                self.adapter.live.answer.assert_awaited_once_with(
                    "connection:1", {"action": "accept", "content": {}}
                )
                interaction.response.send_modal.assert_not_called()
                interaction.message.edit.assert_awaited_once()

    async def test_restricted_and_expired_confirmation_cannot_be_clicked(self):
        _, view = self.request({"type": "object", "properties": {}})
        self.assertFalse(await view.interaction_check(self.interaction(SimpleNamespace(id=1234))))
        self.adapter.live.server_requests.clear()
        self.assertFalse(await view.interaction_check(self.interaction()))
        self.adapter.live.answer.assert_not_called()

    async def test_decline_and_cancel_do_not_accept_permission(self):
        _, view = self.request({"type": "object", "properties": {}})
        for button, action in zip(view.children[1:], ("decline", "cancel")):
            await button.callback(self.interaction())
            self.adapter.live.answer.assert_awaited_with("connection:1", {"action": action})

    async def test_data_and_constrained_schemas_still_need_form(self):
        for schema in (
            {"type": "object", "properties": {"name": {"type": "string"}}},
            {"type": "object", "properties": {}, "minProperties": 1},
            {"type": "object", "properties": {}, "required": ["name"]},
            {"type": "object", "$ref": "https://invalid.example/schema"},
            {"type": "boolean"},
            {},
            False,
        ):
            with self.subTest(schema=schema):
                _, view = self.request(schema, "openai/form")
                self.assertEqual(view.children[0].label, "Fill form")
                self.adapter.live.answer.assert_not_called()

    async def test_boolean_fields_accept_plain_yes_no_without_json(self):
        for value, expected in (("Yes", True), (" no ", False), ("TRUE", True), ("false", False)):
            with self.subTest(value=value):
                schema = {
                    "type": "object",
                    "properties": {"allow": {"type": "boolean"}},
                    "required": ["allow"],
                }
                _, view = self.request(schema)
                interaction = self.interaction()
                await view.children[0].callback(interaction)
                modal = interaction.response.send_modal.call_args.args[0]
                self.assertIn("Yes or no", modal.children[0].placeholder)
                modal.children[0]._value = value
                await modal.on_submit(interaction)
                self.adapter.live.answer.assert_awaited_with(
                    "connection:1", {"action": "accept", "content": {"allow": expected}}
                )

    async def test_invalid_boolean_has_readable_error_and_does_not_answer(self):
        schema = {"type": "object", "properties": {"allow": {"type": "boolean"}}}
        modal = FormModal(self.adapter, "connection:1", schema)
        modal.children[0]._value = "maybe"
        interaction = self.interaction()
        await modal.on_submit(interaction)
        self.assertIn("allow: enter yes or no", interaction.response.send_message.call_args.args[0])
        self.adapter.live.answer.assert_not_called()

    async def test_boolean_values_still_obey_schema_constraints(self):
        schema = {"type": "object", "properties": {"allow": {"type": "boolean", "const": True}}}
        modal = FormModal(self.adapter, "connection:1", schema)
        modal.children[0]._value = "no"
        await modal.on_submit(self.interaction())
        self.adapter.live.answer.assert_not_called()

    async def test_repeated_request_event_posts_once_but_distinct_requests_remain(self):
        self.adapter.events.update_live_status = AsyncMock()
        self.request({"type": "object", "properties": {}})
        event = {
            "method": "chert/inputRequired",
            "requestKey": "connection:1",
            "params": {"threadId": self.session.codex_thread},
        }
        await asyncio.gather(*(self.adapter.events.handle_live_event(dict(event)) for _ in range(2)))
        self.channel.send.assert_awaited_once()
        self.request({"type": "object", "properties": {}}, key="connection:2")
        await self.adapter.events.handle_live_event({**event, "requestKey": "connection:2"})
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_failed_discord_send_can_be_retried(self):
        self.adapter.events.update_live_status = AsyncMock()
        self.request({"type": "object", "properties": {}})
        event = {"requestKey": "connection:1"}
        self.channel.send.side_effect = RuntimeError("unavailable")
        with self.assertRaises(RuntimeError):
            await self.adapter.events.show_request(self.session, event)
        self.channel.send.side_effect = None
        await self.adapter.events.show_request(self.session, event)
        self.assertEqual(self.channel.send.await_count, 2)
