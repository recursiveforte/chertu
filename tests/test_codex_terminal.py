import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backends.codex_terminal import CodexTerminal
from codex_backend import Session


class TerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_tmux_quoted_start_command_is_recognized(self):
        terminal = CodexTerminal('/bin/codex', '/tmp/socket')
        session = Session(1, '/project', 'test', 'session-id', terminal_pane='%1', terminal_command='exec /bin/codex resume session-id')
        terminal.run = AsyncMock(return_value=(0, 'session-id|0|"exec /bin/codex resume session-id"', ''))
        self.assertTrue(await terminal.valid(session))

    async def test_recycled_or_repuposed_pane_is_never_controlled(self):
        terminal = CodexTerminal('/bin/codex', '/tmp/socket')
        session = Session(1, '/project', 'test', 'session-id', terminal_pane='%1', terminal_command='exec /bin/codex resume session-id')
        for metadata in ['other-session|0|exec /bin/codex resume session-id', 'session-id|1|exec /bin/codex resume session-id', 'session-id|0|bash']:
            terminal.run = AsyncMock(return_value=(0, metadata, ''))
            self.assertFalse(await terminal.valid(session))

    async def test_keys_are_validated_before_starting_a_terminal(self):
        terminal = CodexTerminal('/bin/codex', '/tmp/socket')
        terminal.ensure = AsyncMock()
        with self.assertRaises(ValueError):
            await terminal.key(SimpleNamespace(), 'Enter; touch /tmp/injected')
        terminal.ensure.assert_not_called()
