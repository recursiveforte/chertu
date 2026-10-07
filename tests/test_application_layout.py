from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

from chert.paths import ROOT, runtime_path
from chert.vendor import bridge, checkin, storage
from chert.web.dashboard import app


class LayoutTests(unittest.TestCase):
    def test_vendor_boundary_preserves_production_state_and_backup_paths(self):
        self.assertEqual(bridge.STATE_FILE, ROOT / "bot_state.json")
        self.assertIs(bridge.checkin, checkin)
        self.assertIs(bridge.ash_twin, storage)
        self.assertIn(ROOT, storage.PROTECTED)
        backup_paths = {path for path, _, _ in storage.BACKUP_SETS}
        self.assertIn(ROOT / ".env", backup_paths)
        self.assertIn(ROOT / "bot_state.json", backup_paths)

    def test_runtime_paths_are_independent_of_working_directory(self):
        self.assertEqual(runtime_path("private/state.json", ""), ROOT / "private/state.json")
        self.assertEqual(runtime_path("/tmp/custom.json", ""), Path("/tmp/custom.json"))

    def test_both_dashboards_and_static_assets_survive_module_relocation(self):
        client = app.test_client()
        for path in (
            "/codex/",
            "/claudes/",
            "/codex/static/style.css",
            "/claudes/static/style.css",
        ):
            with self.subTest(path=path):
                response = client.get(path)
                try:
                    self.assertEqual(response.status_code, 200)
                finally:
                    response.close()

    def test_existing_offload_command_entrypoint_forwards_arguments(self):
        with (
            patch.object(storage, "main") as main,
            patch("sys.argv", ["ash_twin.py", "restore", "/example"]),
        ):
            runpy.run_path(str(ROOT / "ash_twin.py"), run_name="__main__")
        main.assert_called_once_with(["ash_twin.py", "restore", "/example"])
