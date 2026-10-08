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
        self.assertEqual(thread_title(session.name), "🚀 is-your-src-on-gh")
        self.assertEqual(speaker_name(session), "celeste · is-your-src-on-gh")
        self.assertEqual(thread_title(session.name, ended=True), "🌌 is-your-src-on-gh")

    def test_fixed_title_preserves_collision_suffix_and_length_limit(self):
        self.assertEqual(
            thread_title("same name", sid="abcd", collides=True),
            "🚀 same name · abcd",
        )
        self.assertLessEqual(len(thread_title("x" * 120)), 100)

    def test_turn_summary_uses_elapsed_time_and_known_model(self):
        session = Session(42, "/code/celeste", "test", display_model="test-model")
        body = activity_text(session, "idle", 100, now=165)
        self.assertIn("-# ✅ turn done · 1m 05s", body)
        self.assertIn("🧠 model `test-model`", body)

    def test_waiting_card_matches_reference_layout_and_codex_link(self):
        session = Session(42, "/code/projects", "reply-with-exactly-ok", "native-id",
                          display_model="test-model")
        body = activity_text(session, "waiting", 100, now=104)
        self.assertTrue(body.startswith("**reply-with-exactly-ok** · `projects`\n"))
        self.assertIn("🔔 **needs you** · 0m 04s · updated <t:104:R>", body)
        self.assertIn("🧠 model `test-model`", body)
        self.assertIn("/codex/s/native-id)", body)
        self.assertTrue(body.endswith("reply to talk · `!screen` · `!help`"))
        self.assertNotIn("classifier", body)

    def test_long_progress_preserves_footer_and_message_limit(self):
        session = Session(42, "/code/projects", "test", "native-id")
        session.activity = {"thought": "x" * 3000}
        body = activity_text(session, "running", 100, now=104)
        self.assertLessEqual(len(body), 1990)
        self.assertTrue(body.endswith("`!help`"))

    def test_failed_and_interrupted_turns_never_claim_success(self):
        session = Session(42, "/code/projects", "test")
        for status in ("error", "interrupted", "disconnected"):
            with self.subTest(status=status):
                body = activity_text(session, status, 100, now=104)
                self.assertNotIn("turn done", body)
                self.assertNotIn("**working**", body)
