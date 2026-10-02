import asyncio
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import discord
import discord_bot as upstream
from codex_backend import SessionStore
from codex_bot import Config
from shared_frontend import SharedFrontend
from setup_discord import channels_for


class SharedFrontendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config = Config('test', 100, 7, set(), root, root/'state.json')
        self.bot = SharedFrontend(config, SimpleNamespace(binary='codex'), SessionStore(config.state_file), 200)
        self.bot.owner = 7
        self.bot.codex.owner = 7
        self.bot.codex.main_channel = SimpleNamespace(id=100)
        self.bot.main_channel = SimpleNamespace(id=200)

    async def asyncTearDown(self):
        await self.bot.close()
        self.tmp.cleanup()

    def interaction(self, channel_id):
        return SimpleNamespace(channel=SimpleNamespace(id=channel_id, parent_id=None), channel_id=channel_id,
                               user=SimpleNamespace(id=7), response=SimpleNamespace(defer=AsyncMock(),
                               send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))

    def test_registry_contains_every_upstream_command_and_exact_parameters(self):
        original = upstream.Bridge(intents=discord.Intents.none())
        for command in original.tree.get_commands():
            actual = self.bot.tree.get_command(command.name)
            self.assertIsNotNone(actual, command.name)
            self.assertEqual(list(actual._params), list(command._params), command.name)
            self.assertEqual(actual.default_permissions, command.default_permissions)
        self.assertEqual(len(original.tree.get_commands()), 34)
        self.assertEqual(len(self.bot.tree.get_commands()), 36)

    def test_discord_gateway_dispatcher_is_not_shadowed_by_command_routing(self):
        self.assertIs(SharedFrontend.dispatch, discord.Client.dispatch)
        self.bot.dispatch('socket_event_type', 'READY')

    def test_routes_parent_channels_and_threads_to_only_one_backend(self):
        self.assertEqual(self.bot.backend_for(SimpleNamespace(id=100)), 'codex')
        self.assertEqual(self.bot.backend_for(SimpleNamespace(id=200)), 'claude')
        self.assertEqual(self.bot.backend_for(SimpleNamespace(id=300, parent_id=100)), 'codex')
        self.assertEqual(self.bot.backend_for(SimpleNamespace(id=400, parent_id=200)), 'claude')
        self.assertIsNone(self.bot.backend_for(SimpleNamespace(id=999)))

    async def test_codex_dispatch_cannot_call_claude_handler(self):
        original = AsyncMock()
        self.bot.codex.execute = AsyncMock()
        interaction = self.interaction(100)
        await self.bot.dispatch_command('model', original, interaction, {'name': 'test-model'})
        original.assert_not_called()
        self.bot.codex.execute.assert_awaited_once()

    async def test_claude_dispatch_uses_upstream_handler_without_codex(self):
        original = AsyncMock()
        self.bot.codex.execute = AsyncMock()
        interaction = self.interaction(200)
        await self.bot.dispatch_command('model', original, interaction, {'name': 'test-model'})
        original.assert_awaited_once_with(interaction, name='test-model')
        self.bot.codex.execute.assert_not_called()

    async def test_owner_only_rule_is_identical_across_channels(self):
        for channel in (100, 200):
            interaction = self.interaction(channel)
            interaction.user.id = 999
            original = AsyncMock()
            await self.bot.dispatch_command('model', original, interaction, {'name': 'test'})
            original.assert_not_called()
            interaction.response.send_message.assert_awaited_once()

    def test_both_channels_are_distinct_and_named_as_requested(self):
        channels = channels_for('both')
        self.assertEqual(channels[0][:2], ('codex', 'DISCORD_CODEX_CHANNEL_ID'))
        self.assertEqual(channels[1][:2], ('claude', 'DISCORD_CLAUDE_CHANNEL_ID'))

    async def test_binding_does_not_start_another_discord_gateway(self):
        self.bot.codex.bind_gateway()
        self.assertIs(self.bot.codex.http, self.bot.http)
        self.assertIs(self.bot.codex._connection, self.bot._connection)
        self.assertIs(self.bot._connection._command_tree, self.bot.tree)

    async def test_existing_codex_webhook_is_reused_so_status_edits_keep_working(self):
        hook = SimpleNamespace(id=900, name='chert-codex', token='unused')
        channel = SimpleNamespace(id=100, webhooks=AsyncMock(return_value=[hook]), create_webhook=AsyncMock())
        self.assertIs(await self.bot.webhook_for(channel), hook)
        channel.create_webhook.assert_not_called()

    async def test_every_registered_command_dispatches_through_the_shared_gateway(self):
        self.bot.codex.execute = AsyncMock()
        self.bot.claude.execute = AsyncMock()
        for command in self.bot.tree.get_commands():
            with self.subTest(command=command.name):
                self.bot.codex.execute.reset_mock()
                self.bot.claude.execute.reset_mock()
                params = {name: 'test' for name in command._params}
                await command._do_call(self.interaction(100), params)
                if command.name == 'claude':
                    self.bot.claude.execute.assert_awaited_once()
                    self.bot.codex.execute.assert_not_called()
                else:
                    self.bot.codex.execute.assert_awaited_once()
                    self.bot.claude.execute.assert_not_called()
