"""Browser sessions established only through the email-protected login origin.

The login origin must remain on loopback behind cloudflared --allowed-mail.
The separate public origin never trusts identity headers or mints login grants.
"""

import hashlib
import hmac
import json
import os
import secrets
from urllib.parse import urlencode, urlsplit

from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.wrappers import Request, Response

from chert.file_links import viewer_url
from chert.paths import runtime_path

SESSION_COOKIE = '__Host-chert-viewer-session'
LOGIN_COOKIE = '__Host-chert-viewer-login'
SESSION_SECONDS = 4 * 60 * 60


def login_url_file():
    return runtime_path(os.environ.get('CHERT_LOGIN_URL_FILE'), 'private/file-viewer-url')


def login_config():
    # Written by the protected tunnel with its actual --allowed-mail recipient.
    config = json.loads(login_url_file().with_suffix('.json').read_text())
    expected = os.environ.get('CHERT_TUNNEL_EMAIL', '').strip().lower()
    if not expected or config['email'].lower() != expected:
        raise ValueError('Login policy is not ready.')
    parsed = urlsplit(config['url'])
    if parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith('.trycloudflare.com'):
        raise ValueError('Login origin is not ready.')
    return config


def signer(purpose):
    path = runtime_path(os.environ.get('CHERT_VIEWER_SESSION_KEY'), 'private/viewer-session.key')
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError('Viewer session key must contain exactly 32 random bytes.')
    return URLSafeTimedSerializer(key, salt='chert-viewer-' + purpose)


def public_origin():
    parsed = urlsplit(viewer_url())
    if not parsed.hostname or parsed.scheme != 'https':
        raise ValueError('Public viewer URL is not ready.')
    return f'{parsed.scheme}://{parsed.netloc}'


def safe_next(value):
    return (value if value.startswith('/files/') and not any(
        c in value for c in ['\\', '\r', '\n', '\x00']) else '/files/')


def private_response(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    return response


def redirect(location):
    return private_response(Response('', status=303, headers={'Location': location}))


def begin_login(request):
    config = login_config()
    nonce = secrets.token_urlsafe(32)
    state = signer('login').dumps({'nonce': hashlib.sha256(nonce.encode()).hexdigest(),
                                  'next': safe_next(request.args.get('next', '/files/'))})
    response = redirect(config['url'] + '/auth/complete?' + urlencode({'state': state}))
    response.set_cookie(LOGIN_COOKIE, nonce, max_age=600, secure=True, httponly=True,
                        samesite='Lax', path='/')
    return response


def complete_email_login(request):
    """Only called by the private origin behind the email-protected tunnel."""
    config = login_config()
    state = signer('login').loads(request.args.get('state', ''), max_age=600)
    if not isinstance(state, dict) or not isinstance(state.get('nonce'), str):
        raise BadSignature('Invalid login state')
    grant = signer('grant').dumps({'nonce': state['nonce'], 'next': safe_next(state.get('next', '')),
                                  'email': config['email'].lower()})
    return redirect(public_origin() + '/auth/callback?' + urlencode({'grant': grant}))


def accept_grant(request):
    config = login_config()
    grant = signer('grant').loads(request.args.get('grant', ''), max_age=60)
    nonce = request.cookies.get(LOGIN_COOKIE, '')
    if (not nonce or not isinstance(grant, dict) or
            grant.get('email') != config['email'].lower() or
            not hmac.compare_digest(str(grant.get('nonce', '')), hashlib.sha256(nonce.encode()).hexdigest())):
        raise BadSignature('Login does not belong to this browser')
    response = redirect(safe_next(grant.get('next', '')))
    response.set_cookie(SESSION_COOKIE, signer('session').dumps({'email': grant['email']}),
                        max_age=SESSION_SECONDS, secure=True, httponly=True, samesite='Lax', path='/')
    response.delete_cookie(LOGIN_COOKIE, secure=True, httponly=True, samesite='Lax', path='/')
    return response


def authenticated(request):
    try:
        session = signer('session').loads(request.cookies.get(SESSION_COOKIE, ''), max_age=SESSION_SECONDS)
        return isinstance(session, dict) and session.get('email') == login_config()['email'].lower()
    except BadSignature:
        return False


def login_origin(environ, start_response):
    """Private, protected origin. Never use as the regular viewer tunnel origin."""
    request = Request(environ)
    try:
        if request.method not in {'GET', 'HEAD'}:
            response = private_response(Response('Read-only viewer\n', status=405))
        elif request.path == '/auth/complete':
            response = complete_email_login(request)
        else:
            # Existing file links on the protected hostname move to the browser-session origin.
            response = redirect(public_origin() + safe_next(request.full_path.rstrip('?')))
    except BadSignature:
        response = private_response(Response('This sign-in link expired. Return to the viewer and sign in again.\n', status=403))
    except (OSError, ValueError, KeyError, TypeError):
        response = private_response(Response('Viewer sign-in is temporarily unavailable.\n', status=503))
    return response(environ, start_response)


def app(environ, start_response):
    """Public origin: require a signed session before any file/static/API access."""
    request = Request(environ)
    try:
        # Validate configuration even for unauthenticated API probes.
        signer('session')
        login_config()
        if os.environ.get('CHERT_VIEWER_SESSIONS') != '1':
            raise ValueError('Viewer sessions are not enabled')
        if request.method not in {'GET', 'HEAD'}:
            response = private_response(Response('Read-only viewer\n', status=405))
        elif request.path == '/auth/complete':
            response = private_response(Response('Not found\n', status=404))
        elif request.path == '/login':
            response = begin_login(request)
        elif request.path == '/auth/callback':
            response = accept_grant(request)
        elif request.path == '/logout':
            response = redirect('/files/')
            response.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite='Lax', path='/')
        elif not authenticated(request):
            if request.path.startswith(('/files/api/', '/files/raw/', '/files/static/')):
                response = private_response(Response('{"error":"Sign in to view project files."}', status=401,
                                                     content_type='application/json'))
            else:
                response = redirect('/login?' + urlencode({'next': safe_next(request.full_path.rstrip('?'))}))
            response.headers['X-Chert-Viewer-Auth'] = 'session-required'
        else:
            from chert.web.files_gateway import viewer_app
            environ['chert.viewer_session'] = True
            return viewer_app(environ, start_response)
    except BadSignature:
        response = private_response(Response('This sign-in link expired or belongs to another browser. Open /files/ to try again.\n', status=403))
    except (OSError, ValueError, KeyError, TypeError):
        response = private_response(Response('Viewer sign-in is temporarily unavailable.\n', status=503))
    return response(environ, start_response)
