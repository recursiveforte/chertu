from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from chert.backends.codex.state import Session, SessionStore
from chert.persistence import write_json


class StoreTests(unittest.TestCase):
    def test_failed_serialization_keeps_state_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            write_json(path, {"saved": True})
            before = path.read_bytes()
            with self.assertRaises(TypeError):
                write_json(path, {"invalid": object()}, backup=True)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_failed_replace_preserves_the_original_and_last_good_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            write_json(path, {"saved": True})
            before = path.read_bytes()
            with patch("chert.persistence.os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    write_json(path, {"saved": False}, backup=True)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.with_suffix(".json.bak").read_bytes(), before)
            self.assertEqual(len(list(Path(tmp).iterdir())), 2)

    def test_restart_preserves_identity_but_marks_active_turn_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = SessionStore(path)
            store.sessions[12] = Session(12, tmp, "test", "codex-id", status="running", turns=3)
            store.save()
            loaded = SessionStore(path)
            self.assertEqual(loaded.sessions[12].codex_thread, "codex-id")
            self.assertEqual(loaded.sessions[12].status, "interrupted")
            self.assertEqual(loaded.sessions[12].turns, 3)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_corrupt_state_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{broken")
            with self.assertRaises((ValueError, OSError)):
                SessionStore(path)
            self.assertEqual(path.read_text(), "{broken")

    def test_recovers_last_good_state_after_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = SessionStore(path)
            store.sessions[12] = Session(12, tmp, "test", "codex-id")
            store.save()
            store.sessions[12].turns = 1
            store.save()
            path.write_text("{broken")
            recovered = SessionStore(path)
            self.assertEqual(recovered.sessions[12].codex_thread, "codex-id")
            self.assertEqual(path.with_suffix(".json.bak").stat().st_mode & 0o777, 0o600)
