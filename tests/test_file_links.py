import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from werkzeug.test import Client
from werkzeug.wrappers import Response

from chert.file_links import viewer_context, viewer_url
from chert.discord.frontend import Frontend
from chert.vendor import bridge as upstream
from chert.web.files_gateway import app
from chert import tunnel


class FileLinkContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'projects.json'
        self.url_file = self.root / 'url'
        self.state.write_text(json.dumps({'projects': [
            {'name': 'home', 'directory': str(self.root), 'channel_id': 1},
            {'name': 'notes', 'directory': str(self.root / 'notes'), 'channel_id': 2},
            {'name': 'mac', 'directory': '/Users/me/project', 'channel_id': 3, 'host': 'macbook'},
        ]}))
        env = patch.dict(os.environ, {'PROJECT_STATE_FILE': str(self.state),
                                      'CHERT_FILES_URL_FILE': str(self.url_file), 'CHERT_FILES_URL': ''})
        env.start()
        self.addCleanup(env.stop)

    def test_current_url_is_reread_and_most_specific_project_selected(self):
        self.assertEqual(viewer_context(str(self.root)), '')
        self.url_file.write_text('https://first.trycloudflare.com/files/')
        context = viewer_context(str(self.root / 'notes' / 'docs'))
        self.assertIn('project=notes&path=docs%2Freport.md', context)
        self.assertIn('automatically include clickable viewer links', context)
        self.assertIn('URL-encode both values', context)
        self.url_file.write_text('https://second.trycloudflare.com/files/')
        context = viewer_context(str(self.root / 'notes'))
        self.assertIn('second.trycloudflare.com', context)
        self.assertNotIn('first.trycloudflare.com', context)

    def test_remote_host_scope_and_unavailable_paths(self):
        self.url_file.write_text('https://viewer.example/files/')
        self.assertIn('project=mac', viewer_context('/Users/me/project/src', 'macbook'))
        self.assertEqual(viewer_context('/Users/me/project/src', 'local'), '')
        self.assertEqual(viewer_context('/outside'), '')
        self.assertEqual(viewer_context(str(self.root / 'private' / 'worktree')), '')
        self.assertEqual(viewer_context(str(self.root / '.secret')), '')

    def test_explicit_base_and_invalid_urls(self):
        self.url_file.write_text('https://old.trycloudflare.com/files/')
        with patch.dict(os.environ, {'CHERT_FILES_URL': 'https://files.example/files'}):
            self.assertEqual(viewer_url(), 'https://files.example/files/')
        for value in ['javascript:bad', 'https://user:password@example.com', 'https://example.com/?token=secret', '/files/', 'https://[bad']:
            with self.subTest(value=value), patch.dict(os.environ, {'CHERT_FILES_URL': value}):
                self.assertEqual(viewer_url(), '')

    def test_claude_delivery_includes_current_file_context(self):
        self.url_file.write_text('https://viewer.example/files/')
        with patch.object(upstream.Bridge, 'deliver', return_value=(True, None)) as deliver:
            result = Frontend.deliver(object.__new__(Frontend), {'cwd': str(self.root)}, 'author', 'Make a report')
        self.assertEqual(result, (True, None))
        self.assertIn('Make a report\n\n[Chert file viewer context:', deliver.call_args.args[2])


class FileGatewayTests(unittest.TestCase):
    def test_only_read_only_files_are_exposed(self):
        client = Client(app, Response)
        self.assertEqual(client.get('/').headers['Location'], '/files/')
        response = client.get('/files/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('Sessions ↗', response.text)
        for path in ['/codex/', '/claudes/', '/codex/api/sessions', '/files/../codex/', '/files/static/../../.env']:
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 404)
        for method in ['POST', 'PUT', 'PATCH', 'DELETE']:
            self.assertEqual(client.open('/files/api/demo/document', method=method).status_code, 405)


class ProtectedTunnelTests(unittest.TestCase):
    def test_runner_updates_current_url_and_removes_it_on_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'url'
            path.write_text('https://old.trycloudflare.com/files/')

            def output():
                self.assertFalse(path.exists())
                yield '| https://current-host.trycloudflare.com |\n'
                self.assertEqual(path.read_text(), 'https://current-host.trycloudflare.com/files/\n')

            process = SimpleNamespace(stdout=output(), wait=lambda: 0, poll=lambda: 0)
            with (patch.dict(os.environ, {'CHERT_TUNNEL_EMAIL': 'me@example.com'}),
                  patch.object(tunnel, 'viewer_url_file', return_value=path),
                  patch.object(tunnel, 'load_dotenv'), patch.object(tunnel.signal, 'signal'),
                  patch.object(tunnel.subprocess, 'Popen', return_value=process)):
                self.assertEqual(tunnel.main(), 0)
            self.assertFalse(path.exists())

    def test_cannot_start_an_unprotected_tunnel(self):
        for email in ['', '*@example.com', 'a@example.com,b@example.com', 'missing-domain']:
            with self.subTest(email=email), self.assertRaises(ValueError):
                tunnel.tunnel_command(email)
        command = tunnel.tunnel_command('me@example.com')
        self.assertEqual(command[-2:], ['--allowed-mail', 'me@example.com'])
        self.assertIn('http://127.0.0.1:8900', command)

    def test_only_cloudflare_generated_urls_are_published_privately(self):
        for line in ['https://docs.trycloudflare.com.evil.test/', 'https://evil.example/', 'http://first.trycloudflare.com']:
            self.assertIsNone(tunnel.QUICK_URL.search(line))
        match = tunnel.QUICK_URL.search('| https://quiet-trees.trycloudflare.com |')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'url'
            tunnel.publish_url(path, match.group())
            self.assertEqual(path.read_text(), 'https://quiet-trees.trycloudflare.com/files/\n')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
