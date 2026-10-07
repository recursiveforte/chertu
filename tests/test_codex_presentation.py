import unittest

from chert.backends.codex.state import Session
from chert.backends.codex.presentation import (
    activity_text,
    prompt_name,
    speaker_name,
    thread_title,
)


class PresentationTests(unittest.TestCase):
    def test_original_thread_and_speaker_style(self):
        session = Session(42, "/code/celeste", prompt_name("is your src on gh?"))
        self.assertEqual(thread_title(session.name), "🔥 is-your-src-on-gh")
        self.assertEqual(speaker_name(session), "celeste · is-your-src-on-gh")
        self.assertEqual(thread_title(session.name, ended=True), "🌌 is-your-src-on-gh")

    def test_thread_status_preserves_name_collision_suffix_and_length_limit(self):
        for status, emoji in {
            "running": "🔭", "waiting": "📡", "idle": "🔥", "error": "❌",
            "interrupted": "⏹", "disconnected": "⚪", "ended": "🌌",
        }.items():
            with self.subTest(status=status):
                self.assertEqual(
                    thread_title("same name", status=status, sid="abcd", collides=True),
                    f"{emoji} same name · abcd",
                )
                self.assertLessEqual(len(thread_title("x" * 120, status=status)), 100)

    def test_turn_summary_uses_elapsed_time_and_known_model(self):
        session = Session(42, "/code/celeste", "test", display_model="test-model")
        self.assertEqual(
            activity_text(session, "idle", 100, now=165),
            "-# ✅ turn done · 1m 05s · 🧠 `test-model`",
        )
