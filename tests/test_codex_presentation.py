import unittest

from codex_backend import Session
from codex_presentation import activity_text, avatar_url, prompt_name, speaker_name, thread_title


class PresentationTests(unittest.TestCase):
    def test_original_thread_and_speaker_style(self):
        session = Session(42, '/code/celeste', prompt_name('is your src on gh?'))
        self.assertEqual(thread_title(session.name), '🚀 is-your-src-on-gh')
        self.assertEqual(speaker_name(session), 'celeste · is-your-src-on-gh')
        self.assertEqual(thread_title(session.name, ended=True), '🌌 is-your-src-on-gh')

    def test_turn_summary_uses_elapsed_time_and_known_model(self):
        session = Session(42, '/code/celeste', 'test', display_model='test-model')
        self.assertEqual(activity_text(session, 'idle', 100, now=165),
                         '✅ turn done · 1m 05s · 🧠 `test-model`')

    def test_avatar_identity_survives_rename_and_resume(self):
        session = Session(42, '/code/celeste', 'test')
        original = avatar_url(session)
        session.name, session.codex_thread = 'renamed', 'another-id'
        self.assertEqual(avatar_url(session), original)
