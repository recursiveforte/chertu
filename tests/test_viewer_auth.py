import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from werkzeug.test import Client
from werkzeug.wrappers import Response

from chert.projects import Project
from chert.web import files, viewer_auth as auth
from chert.web.files_gateway import app as protected_app
from chert import tunnel

PUBLIC = 'https://viewer.trycloudflare.com'
LOGIN = 'https://login-test.trycloudflare.com'


class ViewerAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.key = self.root / 'key'
        self.key.write_bytes(os.urandom(32))
        self.login_file = self.root / 'login-url'
        tunnel.publish_url(self.login_file, LOGIN, email='me@example.com')
        (self.root / 'report.md').write_text('# Report\n\nVisible after authentication.')
        self.client = Client(auth.app, Response)
        self.protected = Client(protected_app, Response)
        env = patch.dict(os.environ, {'CHERT_VIEWER_SESSIONS': '1',
            'CHERT_TUNNEL_EMAIL': 'me@example.com', 'CHERT_FILES_URL': PUBLIC + '/files/',
            'CHERT_VIEWER_SESSION_KEY': str(self.key), 'CHERT_LOGIN_URL_FILE': str(self.login_file)})
        env.start()
        self.addCleanup(env.stop)
        projects = patch.object(files, 'projects', return_value={'demo': Project('demo', str(self.root), 1)})
        projects.start()
        self.addCleanup(projects.stop)

    def get(self, path, client=None):
        return (client or self.client).get(path, base_url=PUBLIC)

    def grant(self, next_path='/files/?project=demo&path=report.md'):
        begin = self.get('/login', None) if next_path is None else self.client.get(
            '/login', query_string={'next': next_path}, base_url=PUBLIC)
        self.assertEqual(begin.status_code, 303)
        self.assertIn('Secure', begin.headers['Set-Cookie'])
        self.assertIn('HttpOnly', begin.headers['Set-Cookie'])
        self.assertIn('SameSite=Lax', begin.headers['Set-Cookie'])
        target = urlsplit(begin.headers['Location'])
        self.assertEqual(target.netloc, 'login-test.trycloudflare.com')
        # This private origin is reached only after cloudflared validates the allowed email.
        response = self.protected.get(target.path + '?' + target.query, base_url=LOGIN)
        self.assertEqual(response.status_code, 303)
        grant_url = urlsplit(response.headers['Location'])
        self.assertEqual(grant_url.netloc, 'viewer.trycloudflare.com')
        return grant_url.path + '?' + grant_url.query

    def test_no_files_static_or_identity_headers_bypass_authentication(self):
        for path in ['/files/api/projects', '/files/api/demo/list', '/files/raw/demo?path=report.md', '/files/static/files.js']:
            with self.subTest(path=path):
                response = self.client.get(path, base_url=PUBLIC, headers={
                    'Cf-Access-Authenticated-User-Email': 'me@example.com',
                    'Tailscale-User-Login': 'me@example.com'})
                self.assertEqual(response.status_code, 401)
                self.assertNotIn('report.md', response.text)
        self.assertEqual(self.get('/files/').status_code, 303)
        self.assertEqual(self.get('/auth/complete?state=fake').status_code, 404)

    def test_verified_login_survives_quick_tunnel_cookie_expiry(self):
        stamp = int(time.time())
        with patch('itsdangerous.timed.TimestampSigner.get_timestamp', return_value=stamp):
            grant = self.grant()
            response = self.get(grant)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['Location'], '/files/?project=demo&path=report.md')
        self.assertIn('Max-Age=14400', response.headers.getlist('Set-Cookie')[0])
        # Only Chert's browser cookie is present, not a Cloudflare session cookie.
        for seconds in [6, 30, 600, 14399]:
            with self.subTest(seconds=seconds), patch('itsdangerous.timed.TimestampSigner.get_timestamp', return_value=stamp + seconds):
                response = self.get('/files/api/demo/list')
                self.assertEqual(response.status_code, 200)
                self.assertIn('report.md', response.text)
                self.assertEqual(self.get('/files/api/demo/document?path=report.md').status_code, 200)
        with patch('itsdangerous.timed.TimestampSigner.get_timestamp', return_value=stamp + 14401):
            self.assertEqual(self.get('/files/api/demo/list').status_code, 401)

    def test_stolen_grant_wrong_browser_replay_and_tampering_fail(self):
        grant = self.grant()
        other = Client(auth.app, Response)
        self.assertEqual(self.get(grant, other).status_code, 403)
        other.get('/login', base_url=PUBLIC)
        self.assertEqual(self.get(grant, other).status_code, 403)
        self.assertEqual(self.get(grant + 'tampered').status_code, 403)
        self.assertEqual(self.get(grant).status_code, 303)
        self.assertEqual(self.get(grant).status_code, 403)

    def test_old_grants_and_invalid_state_are_rejected(self):
        stamp = int(time.time())
        with patch('itsdangerous.timed.TimestampSigner.get_timestamp', return_value=stamp):
            grant = self.grant()
        with patch('itsdangerous.timed.TimestampSigner.get_timestamp', return_value=stamp + 61):
            self.assertEqual(self.get(grant).status_code, 403)
        self.assertEqual(self.protected.get('/auth/complete?state=forged', base_url=LOGIN).status_code, 403)

    def test_no_open_redirect_and_old_links_move_to_public_viewer(self):
        for path in ['https://evil.example', '//evil.example', '/files/\\evil', '/files/\nheader']:
            with self.subTest(path=path):
                self.assertEqual(self.get(self.grant(path)).headers['Location'], '/files/')
        response = self.protected.get('/files/?project=demo&path=report.md', base_url=LOGIN)
        self.assertEqual(response.headers['Location'], PUBLIC + '/files/?project=demo&path=report.md')

    def test_missing_key_disabled_sessions_and_mismatched_email_fail_closed(self):
        self.key.unlink()
        self.assertEqual(self.get('/files/api/projects').status_code, 503)
        self.key.write_bytes(os.urandom(32))
        with patch.dict(os.environ, {'CHERT_VIEWER_SESSIONS': '0'}):
            self.assertEqual(self.get('/files/api/projects').status_code, 503)
        with patch.dict(os.environ, {'CHERT_TUNNEL_EMAIL': 'someone-else@example.com'}):
            self.assertEqual(self.get('/login').status_code, 503)
            self.assertEqual(self.get('/files/api/projects').status_code, 503)

    def test_logout_key_rotation_and_route_limits(self):
        self.get(self.grant())
        for path in ['/codex/', '/claudes/', '/codex/api/sessions']:
            self.assertEqual(self.get(path).status_code, 404)
        self.assertEqual(self.client.post('/files/api/demo/document', base_url=PUBLIC).status_code, 405)
        self.key.write_bytes(os.urandom(32))
        self.assertEqual(self.get('/files/api/projects').status_code, 401)
        self.get(self.grant())
        self.get('/logout')
        self.assertEqual(self.get('/files/api/projects').status_code, 401)

    def test_tunnel_preflight_refuses_an_unprotected_origin(self):
        import io
        from urllib.response import addinfourl
        for code, marker in [(200, None), (200, 'session-required'), (401, None), (503, None)]:
            with self.subTest(code=code, marker=marker):
                response = addinfourl(io.BytesIO(b''), {'X-Chert-Viewer-Auth': marker}, '', code)
                with patch.object(tunnel.urllib.request, 'urlopen', return_value=response):
                    with self.assertRaises(ValueError):
                        tunnel.tunnel_command('me@example.com', viewer=True)
        response = addinfourl(io.BytesIO(b''), {'X-Chert-Viewer-Auth': 'session-required'}, '', 401)
        with patch.object(tunnel.urllib.request, 'urlopen', return_value=response):
            command = tunnel.tunnel_command('me@example.com', viewer=True)
        self.assertIn('http://127.0.0.1:8902', command)
        self.assertNotIn('--allowed-mail', command)
