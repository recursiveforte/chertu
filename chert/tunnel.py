"""Run an email-protected Quick Tunnel and publish its current viewer URL."""

import os
import re
import signal
import subprocess
import json
import sys
import urllib.request
import urllib.error

from dotenv import load_dotenv
from chert.paths import ROOT
from chert.file_links import viewer_url_file
from chert.web.viewer_auth import login_url_file, login_config, signer

QUICK_URL = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com(?=[\s/|\"']|$)")


def tunnel_command(email, viewer=False):
    # Never silently fall back to an unauthenticated Quick Tunnel.
    if not re.fullmatch(r"[^\s,@*]+@[^\s,@*]+\.[^\s,@*]+", email):
        raise ValueError("Set CHERT_TUNNEL_EMAIL to one allowed email address.")
    command = [os.environ.get("CLOUDFLARED_BIN") or "cloudflared", "tunnel",
            "--no-autoupdate", "--protocol", "http2", "--edge-ip-version", "4",
            "--url", "http://127.0.0.1:8902" if viewer else "http://127.0.0.1:8900"]
    if viewer:
        verify_session_origin()
    else:
        command.extend(["--allowed-mail", email])
    return command


def verify_session_origin():
    if os.environ.get('CHERT_VIEWER_SESSIONS') != '1':
        raise ValueError('The viewer tunnel requires authenticated browser sessions.')
    signer('session')
    login_config()
    try:
        response = urllib.request.urlopen('http://127.0.0.1:8902/files/api/projects', timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        if response.status != 401 or response.headers.get('X-Chert-Viewer-Auth') != 'session-required':
            raise ValueError('Refusing to expose a viewer origin without authentication.')


def publish_url(path, value, email=None):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(value + "/files/\n")
    temporary.replace(path)
    if email:
        policy = path.with_suffix('.json')
        temporary = policy.with_suffix('.tmp')
        with temporary.open('w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump({'url': value, 'email': email.lower()}, stream)
        temporary.replace(policy)


def main(viewer=False):
    load_dotenv(ROOT / ".env")
    path = viewer_url_file() if viewer else login_url_file()
    path.unlink(missing_ok=True)
    email = os.environ.get("CHERT_TUNNEL_EMAIL", "").strip()
    command = tunnel_command(email, viewer=viewer)
    # The tunnel needs no Discord token, agent credentials, or dashboard secrets.
    child_env = {key: value for key, value in os.environ.items()
                 if key in {"PATH", "HOME", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, bufsize=1, env=child_env)

    def stop(_signum, _frame):
        process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for line in process.stdout:
            match = QUICK_URL.search(line)
            if match:
                publish_url(path, match.group(), email=None if viewer else email)
            print(line, end="", flush=True)
        return process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        path.unlink(missing_ok=True)
        if not viewer:
            path.with_suffix('.json').unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main(viewer='--viewer' in sys.argv))
