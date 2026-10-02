import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import setup_discord


class SetupTests(unittest.TestCase):
    def test_backend_channels(self):
        self.assertEqual(setup_discord.channels_for('codex')[0][0], 'codex')
        self.assertEqual(len(setup_discord.channels_for('claude')), 3)
        with self.assertRaises(ValueError):
            setup_discord.channels_for('invalid')

    def test_private_env_preserves_existing_settings_and_quoted_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            with patch.object(setup_discord, 'ENV', path):
                setup_discord.write_env({'DISCORD_BOT_TOKEN': 'fake-token',
                                        'CODEX_BIN': '"/path with spaces/codex"'})
                setup_discord.write_env({'DISCORD_CHANNEL_ID': '123'})
                env = setup_discord.read_env()
                self.assertEqual(env['DISCORD_BOT_TOKEN'], 'fake-token')
                self.assertEqual(env['CODEX_BIN'], '/path with spaces/codex')
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_dry_run_does_not_create_env_or_virtualenv(self):
        source = Path(__file__).resolve().parents[1] / 'setup.sh'
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / 'setup.sh'
            copy.write_bytes(source.read_bytes())
            result = subprocess.run(['bash', str(copy), '--dry-run'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ['setup.sh'])
