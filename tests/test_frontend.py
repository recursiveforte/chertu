from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from chert.vendor import bridge as upstream
from chert.backends.codex.state import Session, SessionStore
from chert.config import Config
from support import make_frontend


class FrontendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config = Config("test", 1, 7, set(), root, root / "state.json")
        self.bot = make_frontend(
            config, SimpleNamespace(binary="codex"), SessionStore(config.state_file), 200
        )
        self.bot.owner = 7
        self.bot.codex.owner = 7
        self.bot.codex.main_channel = self.bot.project_channels[100]
        self.bot.main_channel = self.bot.project_channels[100]

    async def asyncTearDown(self):
        await self.bot.close()
        self.tmp.cleanup()

    def interaction(self, channel_id):
        return SimpleNamespace(
            channel=SimpleNamespace(id=channel_id, parent_id={300: 100, 400: 200}.get(channel_id)),
            channel_id=channel_id,
            user=SimpleNamespace(id=7),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    def test_registry_contains_every_upstream_command_and_exact_parameters(self):
        original = upstream.Bridge(intents=discord.Intents.none())
        for command in original.tree.get_commands():
            if command.name in {"all", "hub", "restartall", "reviveall", "cleanup"}:
                continue
            actual = self.bot.tree.get_command(command.name)
            self.assertIsNotNone(actual, command.name)
            self.assertEqual(
                list(actual._params), [p for p in command._params if p != "project"], command.name
            )
            self.assertEqual(actual.default_permissions, command.default_permissions)
        self.assertEqual(len(original.tree.get_commands()), 34)
        self.assertEqual(len(self.bot.tree.get_commands()), 35)

    def test_discord_gateway_dispatcher_is_not_shadowed_by_command_routing(self):
        self.assertIs(type(self.bot).dispatch, discord.Client.dispatch)
        self.bot.dispatch("socket_event_type", "READY")

    async def test_model_name_is_optional_in_discord_schema_and_argument_parser(self):
        command = self.bot.tree.get_command("model")
        option = command.to_dict(self.bot.tree)["options"][0]
        self.assertFalse(option["required"])
        arguments = await command._transform_arguments(self.interaction(300), SimpleNamespace())
        self.assertEqual(arguments, {"name": ""})
        self.assertTrue(self.bot.tree.get_command("globalmodel")._params["name"].required)

    async def open_model_picker(self):
        session = Session(
            300,
            self.tmp.name,
            "test",
            "native",
            backend="app-server",
            pending_settings={"model": "next-model"},
        )
        self.bot.codex.store.sessions[300] = session

        async def call(method, params):
            if method == "model/list":
                return {
                    "data": [
                        {"model": model, "displayName": model}
                        for model in ("current-model", "next-model")
                    ]
                }
            return {"thread": {"model": "current-model"}}

        self.bot.codex.live = SimpleNamespace(
            connect=AsyncMock(), close=AsyncMock(), call=AsyncMock(side_effect=call)
        )
        interaction = self.interaction(300)
        await self.bot.tree.get_command("model")._do_call(interaction, {})
        return interaction, interaction.followup.send.call_args.kwargs["view"]

    async def test_bare_model_shows_current_and_queued_models_without_changing_them(self):
        interaction, view = await self.open_model_picker()
        body = interaction.followup.send.call_args.args[0]
        self.assertIn("**Current model:** `current-model`", body)
        self.assertIn("**Queued model:** `next-model`", body)
        self.assertTrue(interaction.followup.send.call_args.kwargs["ephemeral"])
        self.assertEqual([o.value for o in view.select.options if o.default], ["next-model"])
        self.assertEqual(
            [c.args[0] for c in self.bot.codex.live.call.call_args_list],
            ["model/list", "thread/read"],
        )

    async def test_model_picker_selection_uses_existing_backend_handler(self):
        _, view = await self.open_model_picker()
        self.bot.codex.controls.execute = AsyncMock()
        click = self.interaction(300)
        click.message = SimpleNamespace(edit=AsyncMock())
        view.select._values = ["next-model"]
        self.assertTrue(await view.interaction_check(click))
        await view.choose(click)
        args = self.bot.codex.controls.execute.call_args.args
        self.assertEqual((args[0], args[3]), ("model", {"name": "next-model"}))
        click.message.edit.assert_awaited_once_with(content="Selected `next-model`.", view=None)
        self.assertTrue(view.is_finished())

    async def test_model_picker_rejects_another_user_or_thread(self):
        _, view = await self.open_model_picker()
        for channel, user in ((300, 99), (400, 7)):
            click = self.interaction(channel)
            click.user.id = user
            self.assertFalse(await view.interaction_check(click))
            click.response.send_message.assert_awaited_once()

    async def test_bare_model_in_parent_channel_does_not_change_global_default(self):
        interaction = self.interaction(100)
        self.bot.codex.controls.execute = AsyncMock()
        await self.bot.tree.get_command("model")._do_call(interaction, {})
        self.assertIn("session thread", interaction.response.send_message.call_args.args[0])
        self.bot.codex.controls.execute.assert_not_called()

    async def test_bare_model_keeps_disabled_claude_disabled(self):
        self.bot.claude_enabled = False
        interaction = self.interaction(200)
        await self.bot.tree.get_command("model")._do_call(interaction, {})
        self.assertIn("disabled", interaction.response.send_message.call_args.args[0])

    async def test_codex_dispatch_cannot_call_claude_handler(self):
        original = AsyncMock()
        self.bot.codex.controls.execute = AsyncMock()
        interaction = self.interaction(100)
        await self.bot.dispatch_command("model", original, interaction, {"name": "test-model"})
        original.assert_not_called()
        self.bot.codex.controls.execute.assert_awaited_once()

    async def test_claude_dispatch_uses_upstream_handler_without_codex(self):
        original = AsyncMock()
        self.bot.codex.controls.execute = AsyncMock()
        interaction = self.interaction(200)
        await self.bot.dispatch_command("model", original, interaction, {"name": "test-model"})
        original.assert_awaited_once_with(interaction, name="test-model")
        self.bot.codex.controls.execute.assert_not_called()

    async def test_owner_only_rule_is_identical_across_channels(self):
        for channel in (100, 200):
            interaction = self.interaction(channel)
            interaction.user.id = 999
            original = AsyncMock()
            await self.bot.dispatch_command("model", original, interaction, {"name": "test"})
            original.assert_not_called()
            interaction.response.send_message.assert_awaited_once()

    async def test_binding_does_not_start_another_discord_gateway(self):
        self.assertNotIsInstance(self.bot.codex, discord.Client)
        self.bot.fetch_channel = AsyncMock(return_value=SimpleNamespace(id=123))
        self.assertEqual((await self.bot.codex.fetch_channel(123)).id, 123)
        self.bot.fetch_channel.assert_awaited_once_with(123)
        self.assertIs(self.bot._connection._command_tree, self.bot.tree)

    async def test_host_disk_alert_goes_to_codex_without_changing_claude_channel(self):
        self.bot.claude_enabled = False
        self.bot.say = AsyncMock()
        fake_disk = SimpleNamespace(disk_free=Mock(return_value=(170, 13)))
        with (
            patch.object(upstream, "ash_twin", fake_disk),
            patch.object(upstream, "save_state"),
            patch.dict(upstream.state, {"_meta": {"disk_level": "crit"}}),
        ):
            await self.bot.disk_tick()
        self.assertIs(self.bot.say.call_args.args[0], self.bot.codex.main_channel)
        self.assertEqual(self.bot.main_channel.id, 100)

    async def test_disabled_claude_ignores_hooks_and_rejects_dashboard_restart(self):
        self.bot.claude_enabled = False
        with patch.object(upstream.Bridge, "on_hook", new_callable=AsyncMock) as hook:
            await self.bot.on_hook({"hook_event_name": "SessionStart"})
            hook.assert_not_called()
        response = await self.bot.admin_restart_all(SimpleNamespace())
        self.assertEqual(response.status, 503)

    async def test_every_registered_command_dispatches_through_the_shared_gateway(self):
        self.bot.codex.controls.execute = AsyncMock()
        self.bot.claude.command = AsyncMock()
        for command in self.bot.tree.get_commands():
            if command.name in {
                "project",
                "harness",
                "archive",
                "unarchive",
                "sessions",
                "codex",
                "claude",
                "astra",
            }:
                continue
            with self.subTest(command=command.name):
                self.bot.codex.controls.execute.reset_mock()
                self.bot.claude.command.reset_mock()
                params = {name: "test" for name in command._params}
                await command._do_call(self.interaction(100), params)
                if command.name == "claude":
                    self.bot.claude.command.assert_awaited_once()
                    self.bot.codex.controls.execute.assert_not_called()
                else:
                    self.bot.codex.controls.execute.assert_awaited_once()
                    self.bot.claude.command.assert_not_called()
