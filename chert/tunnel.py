"""Run an email-protected Quick Tunnel and publish its current viewer URL."""

import os
import re
import signal
import subprocess

from dotenv import load_dotenv
from chert.paths import ROOT
from chert.file_links import viewer_url_file

QUICK_URL = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com(?=[\s/|\"']|$)")


def tunnel_command(email):
    # Never silently fall back to an unauthenticated Quick Tunnel.
    if not re.fullmatch(r"[^\s,@*]+@[^\s,@*]+\.[^\s,@*]+", email):
        raise ValueError("Set CHERT_TUNNEL_EMAIL to one allowed email address.")
    return [os.environ.get("CLOUDFLARED_BIN") or "cloudflared", "tunnel",
            "--no-autoupdate", "--protocol", "http2", "--edge-ip-version", "4",
            "--url", "http://127.0.0.1:8900", "--allowed-mail", email]


def publish_url(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(value + "/files/\n")
    temporary.replace(path)


def main():
    load_dotenv(ROOT / ".env")
    path = viewer_url_file()
    path.unlink(missing_ok=True)
    command = tunnel_command(os.environ.get("CHERT_TUNNEL_EMAIL", "").strip())
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
                publish_url(path, match.group())
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


if __name__ == "__main__":
    raise SystemExit(main())
