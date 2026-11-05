"""Opt-in browser checks: install Playwright, then run with optional --cdp URL.

An isolated loopback origin simulates Access expiry, network errors, and recovery.
No production credentials or project files are used.
"""

import argparse
import logging
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from werkzeug.wrappers import Response

from chert.projects import Project
from chert.web import files
from chert.web.dashboard import app


def main(cdp=None):
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    with tempfile.TemporaryDirectory(prefix='chert-browser-files-') as temporary:
        root = Path(temporary)
        (root / 'report.md').write_text('# Report\n\nA readable document.\n')
        files.projects = lambda: {'demo': Project('demo', str(root), 1)}
        state = {'failure': 'redirect', 'requests': 0}

        def origin(environ, start_response):
            if environ.get('PATH_INFO', '').startswith('/files/api/'):
                state['requests'] += 1
                if state['failure'] == 'redirect':
                    return Response('', status=302, headers={
                        'Location': 'http://127.0.0.1:18999/login'
                    })(environ, start_response)
                if state['failure'] == 'upstream':
                    return Response('<h1>Bad gateway</h1>', status=502,
                                    content_type='text/html')(environ, start_response)
                if state['failure'] == 'private':
                    return Response('{"error":"This file is private."}', status=403,
                                    content_type='application/json')(environ, start_response)
            return app(environ, start_response)

        server = make_server('127.0.0.1', 8901, origin, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as playwright:
                browser = (playwright.chromium.connect_over_cdp(cdp) if cdp
                           else playwright.chromium.launch())
                context = browser.new_context()
                try:
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    target = 'http://127.0.0.1:8901/files/?project=demo&path=report.md'
                    page.goto(target)
                    expect(page.locator('#connection-message')).to_contain_text('sign-in expired')
                    expect(page.locator('#sign-in')).to_have_attribute('href', target)
                    # The auth-specific notice establishes an opaque redirect response;
                    # a followed cross-origin redirect would instead be a network failure.
                    assert page.locator('#project').is_disabled()

                    state['failure'] = ''
                    page.locator('#retry').click()
                    expect(page.locator('#content h1')).to_have_text('Report')
                    expect(page.locator('#connection-notice')).to_be_hidden()

                    state['failure'] = 'redirect'
                    expect(page.locator('#connection-notice')).to_be_visible(timeout=8000)
                    count = state['requests']
                    page.wait_for_timeout(5500)
                    assert state['requests'] == count, 'polling continued after authentication expired'
                    expect(page.locator('#content h1')).to_have_text('Report')

                    state['failure'] = 'upstream'
                    page.locator('#retry').click()
                    expect(page.locator('#connection-message')).to_contain_text('HTTP 502')
                    expect(page.locator('#sign-in')).to_be_hidden()

                    state['failure'] = ''
                    page.locator('#retry').click()
                    expect(page.locator('#connection-notice')).to_be_hidden()
                    expect(page.locator('#content h1')).to_have_text('Report')

                    state['failure'] = 'private'
                    page.locator('#reload').click()
                    expect(page.locator('#list-status')).to_have_text('This file is private.')
                    expect(page.locator('#connection-notice')).to_be_hidden()

                    state['failure'] = ''
                    page.route('**/files/api/**', lambda route: route.abort())
                    page.reload()
                    expect(page.locator('#connection-message')).to_contain_text('Check your connection')
                    expect(page.locator('#sign-in')).to_be_hidden()
                    page.unroute('**/files/api/**')
                    page.locator('#retry').click()
                    expect(page.locator('#content h1')).to_have_text('Report')
                    assert not errors, errors
                    print('PASS: expired login, preserved document/link, paused polling, first-load retry, '
                          'network recovery, HTML upstream failures, and private-file errors.')
                finally:
                    context.close()
                    browser.close()
        finally:
            server.shutdown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--cdp')
    main(parser.parse_args().cdp)
