import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import dashboard
from backends import codex_storage

SID = '11111111-1111-4111-8111-111111111111'


class DashboardTests(unittest.TestCase):
    def test_same_templates_render_codex_and_allow_composer_without_fake_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)/'state.json'
            state.write_text(json.dumps({'sessions': [{'codex_thread': SID, 'cwd': tmp, 'name': 'test',
                                                       'status': 'idle', 'recent': []}]}))
            with patch.object(dashboard, 'STATE', state):
                client = dashboard.app.test_client()
                page = client.get(f'/codex/s/{SID}')
                self.assertEqual(page.status_code, 200)
                html = page.get_data(as_text=True)
                self.assertIn('id="msg"', html)
                self.assertIn('id="screen"', html)
                self.assertIn('data-key="Up"', html)
                self.assertEqual(client.get('/codex/api/sessions').json['sessions'][0]['sid'], SID)

    def test_codex_journal_uses_the_original_transcript_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)/'state.json'
            state.write_text(json.dumps({'sessions': [{'codex_thread': SID, 'cwd': tmp, 'name': 'test', 'status': 'idle'}]}))
            folder = Path(tmp)/'codex-transcripts'
            folder.mkdir()
            (folder/f'{SID}.jsonl').write_text(json.dumps({'type': 'assistant', 'message': {
                'content': [{'type': 'text', 'text': 'Hello <script>bad()</script>'}]}})+'\n')
            with patch.object(dashboard, 'STATE', state):
                response = dashboard.app.test_client().get(f'/codex/api/transcript/{SID}')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json['items'][0]['kind'], 'assistant')
                self.assertNotIn('<script>', response.json['items'][0]['html'])


class BackupRestoreTests(unittest.TestCase):
    def test_restores_only_matching_identity_and_never_overwrites_local_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            row = {'relative': f'sessions/2026/10/02/rollout-{SID}.jsonl', 'key': 'backup-key'}
            def download(bucket, key, filename):
                Path(filename).write_text(json.dumps({'type': 'session_meta', 'payload': {'id': SID}})+'\n')
            client = SimpleNamespace(download_file=download)
            with patch.object(codex_storage, 'backup_index', return_value={SID: row}), \
                 patch.object(codex_storage, 'codex_home', return_value=home), \
                 patch.object(codex_storage.ash_twin, '_s3', return_value=client):
                self.assertTrue(codex_storage.restore(SID))
                path = home/row['relative']
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                path.write_text('newer history')
                self.assertFalse(codex_storage.restore(SID))
                self.assertEqual(path.read_text(), 'newer history')

    def test_rejects_backup_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = {'relative': '../escape.jsonl', 'key': 'key'}
            with patch.object(codex_storage, 'backup_index', return_value={SID: row}), \
                 patch.object(codex_storage, 'codex_home', return_value=Path(tmp)):
                with self.assertRaises(ValueError):
                    codex_storage.restore(SID)
