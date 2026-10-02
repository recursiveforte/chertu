"""Pin the audited source of truth, not another handwritten feature list."""
import hashlib
from pathlib import Path
import unittest


class UpstreamContractTests(unittest.TestCase):
    def test_original_bridge_and_parser_remain_the_audited_upstream_versions(self):
        root = Path(__file__).resolve().parents[1]
        expected = {
            'discord_bot.py': '3d5f0a937d1be6c8690ff76c30dfc8d0bff26e86d826609eb413c069aca91bd0',
            'app.py': '1f9d2476f93865e0105b718e045b007519711c381405a351fda9e5e4c993ca3a',
        }
        for name, digest in expected.items():
            self.assertEqual(hashlib.sha256((root/name).read_bytes()).hexdigest(), digest,
                             f'{name} changed: re-audit against upstream before updating this pin')
