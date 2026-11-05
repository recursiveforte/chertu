# File viewer links and remote access

The regular dashboard serves the viewer at `/files/`. Every Codex prompt (new,
queued, or steered) and every Claude delivery receives file-link instructions
when a public viewer URL is configured and the session directory is accessible
through a registered project. This also updates existing conversations on their
next prompt. Claude remains disabled unless explicitly enabled separately.

The context selects the most specific registered directory on the session's
host and tells the agent to include links automatically for files it creates or
updates. URLs use the viewer base plus URL-encoded `project` and `path` query
parameters. Paths are relative to the registered project, not the session's
working directory. Hidden/private directories and worktrees outside viewable
project roots are not advertised as accessible.

## Public URL configuration

- `CHERT_FILES_URL`: a fixed viewer base, including `/files/`, for an existing
  authenticated deployment. This takes precedence over the generated URL file.
- `CHERT_FILES_URL_FILE`: defaults to `private/file-viewer-url`; read on every
  prompt, so tunnel changes do not require restarting the bridge.

The public URL contains no authentication secret. Authentication stays in the
browser, rather than in agent prompts or copied links.

## Protected Cloudflare Quick Tunnel

Cloudflare's [email-protected Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/#restrict-access-by-email)
work without a domain or account. The visitor signs in with a one-time email
code. Use a current `cloudflared` version supporting `--allowed-mail` (verified
with 2026.10.7).

Set `CHERT_TUNNEL_EMAIL` to the single allowed email and `CLOUDFLARED_BIN` to the
installed executable. Leave `CHERT_FILES_URL` blank. Render and install the two
optional systemd templates, substituting `@USER@`, `@APP@`, and `@VENV@` for the
deployment user, checkout, and virtual environment:

- `systemd/templates/chert-files.service.in`: runs the viewer-only WSGI gateway
  on `127.0.0.1:8900`. It allows only GET/HEAD requests beneath `/files/` and does
  not expose conversation transcripts or session-control endpoints.
- `systemd/templates/chert-files-tunnel.service.in`: runs `python -m chert.tunnel`,
  requiring `CHERT_TUNNEL_EMAIL`. The command always includes `--allowed-mail`;
  missing or wildcard recipients fail startup rather than expose an open tunnel.

After installation, run `sudo systemctl daemon-reload` and
`sudo systemctl enable --now chert-files chert-files-tunnel`. Read the current
viewer link from `private/file-viewer-url`. Keep both HTTP listeners on loopback;
the tunnel's email authentication is the external access boundary.

The runner publishes its current URL atomically and removes it when the tunnel
exits. The service restarts after failures and reboots. **Quick Tunnel hostnames
change after a restart, so previously posted links expire.** Subsequent prompts
receive the new URL automatically. Cloudflare provides no uptime guarantee for
Quick Tunnels; configure a named tunnel and `CHERT_FILES_URL` for a stable domain.

Cloudflare has an open [protected Quick Tunnel session-expiry issue](https://github.com/cloudflare/cloudflared/issues/1752):
some email sign-ins expire within seconds, redirecting later file requests to
login. Chert detects those redirects without following them as cross-origin
fetches, pauses polling, preserves the open document, and offers **Sign in again**.
Connection failures have a separate retry action, including during initial
project loading. This improves recovery and diagnosis; it does not repair the
Cloudflare broker's session lifetime. Do not remove email protection as a fix.

## Verification

Run `python -m unittest discover -s tests -v` in an isolated checkout. The tests
cover local/remote project selection, URL rotation, native queue/steering input,
Claude delivery, excluded paths, the gateway's method/route limits, and mandatory
email protection. `python tests/live_codex_activity.py --files` performs a real
isolated agent turn and checks that a newly written report receives the correct
viewer link without explicitly requesting one. It uses an isolated `CODEX_HOME`
and in-memory Discord sink; no existing conversation is used.

`python tests/browser_file_viewer.py` (with Playwright installed) checks login
redirects, paused polling, preserved document links, first-load recovery, network
errors, and non-JSON upstream responses using a temporary origin. `--cdp URL`
can use a separate temporary browser instead of a local Playwright browser.

Before exposing project files, test the tunnel against an empty origin and
verify its unauthenticated URL reaches Cloudflare's email sign-in form. Verify
both `/files/` and `/files/api/projects` are protected. The intended user must
complete the email sign-in; do not retrieve or share their login code.
