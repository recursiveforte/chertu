"""Opt-in browser test for the two-origin login handoff, using only test identities."""

import argparse
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from werkzeug.wrappers import Request, Response
from chert.web import viewer_auth as auth, files
from chert.web.files_gateway import app as protected_app
from chert.projects import Project


def main(cdp=None):
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    with tempfile.TemporaryDirectory(prefix='chert-viewer-auth-browser-') as tmp:
        root = Path(tmp)
        key = root / 'key'
        key.write_bytes(os.urandom(32))
        (root / 'docs').mkdir()
        (root / 'docs' / 'report.md').write_text('# Browser session\n\nFolder reads still work.\n')
        protected_requests = []

        def email_protected_origin(environ, start_response):
            request = Request(environ)
            protected_requests.append(request.path)
            # Simulate the existing email gate and its five-second cookie lifetime.
            # This test origin is never connected to a public tunnel.
            if request.cookies.get('test_email_verified') != 'yes':
                return Response('Email sign-in required', status=403)(environ, start_response)
            return protected_app(environ, start_response)

        with (patch.dict(os.environ, {'CHERT_VIEWER_SESSIONS': '1', 'CHERT_VIEWER_SESSION_KEY': str(key)}),
              patch.object(auth, 'login_config', return_value={'url': 'http://127.0.0.1:8904', 'email': 'test@example.com'}),
              patch.object(auth, 'public_origin', return_value='http://localhost:8903'),
              patch.object(files, 'projects', return_value={'demo': Project('demo', str(root), 1)})):
            servers = [make_server('127.0.0.1', 8903, auth.app, threaded=True),
                       make_server('127.0.0.1', 8904, email_protected_origin, threaded=True)]
            for server in servers:
                threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with sync_playwright() as p:
                    browser = p.chromium.connect_over_cdp(cdp) if cdp else p.chromium.launch()
                    context = browser.new_context()
                    try:
                        context.add_cookies([{'name': 'test_email_verified', 'value': 'yes',
                                             'domain': '127.0.0.1', 'path': '/', 'expires': time.time() + 5}])
                        page = context.new_page()
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.goto('http://localhost:8903/files/?project=demo&path=docs/report.md')
                        expect(page.locator('#content h1')).to_have_text('Browser session')
                        assert len(protected_requests) == 1
                        cookies = context.cookies()
                        session = next(c for c in cookies if c['name'] == auth.SESSION_COOKIE)
                        assert session['httpOnly'] and session['secure'] and session['sameSite'] == 'Lax'
                        assert not any(c['name'] == auth.LOGIN_COOKIE for c in cookies)
                        page.wait_for_timeout(6500)
                        assert not any(c['name'] == 'test_email_verified' for c in context.cookies())
                        page.locator('#up').click()
                        expect(page.locator('.entry', has_text='docs')).to_be_visible()
                        page.locator('.entry', has_text='docs').click()
                        page.locator('.entry', has_text='report.md').click()
                        expect(page.locator('#status')).to_have_text('Up to date')
                        page.reload()
                        expect(page.locator('#content h1')).to_have_text('Browser session')
                        assert len(protected_requests) == 1, 'File browsing depended on the expired email cookie'
                        assert page.request.get('http://localhost:8903/files/api/demo/list').status == 200
                        stranger = browser.new_context()
                        try:
                            assert stranger.request.get('http://localhost:8903/files/api/demo/list').status == 401
                        finally:
                            stranger.close()
                        page.goto('http://localhost:8903/logout')
                        assert 'Email sign-in required' in page.locator('body').inner_text()
                        assert not errors, errors
                        print('PASS: browser-bound login across origins, directory browsing/refresh after '
                              'email-cookie expiry, persistent reload, anonymous denial, and logout.')
                    finally:
                        context.close()
                        browser.close()
            finally:
                for server in servers:
                    server.shutdown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--cdp')
    main(parser.parse_args().cdp)
