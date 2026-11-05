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

## Email login with persistent browser sessions

Cloudflare's [email-protected Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/#restrict-access-by-email)
work without a domain or account. The visitor signs in with a one-time email
code. Use a current `cloudflared` version supporting `--allowed-mail` (verified
with 2026.10.7).

Cloudflare has an open [protected Quick Tunnel session-expiry issue](https://github.com/cloudflare/cloudflared/issues/1752):
some email sign-ins expire within seconds. To keep directory browsing working,
Chert uses the protected tunnel only for the email check and establishes its own
four-hour browser session on a separate viewer hostname. File requests no longer
depend on the short-lived Cloudflare cookie. The email restriction is retained.

Set these values in the private environment file:

```dotenv
CHERT_TUNNEL_EMAIL=your-allowed-email
CLOUDFLARED_BIN=/path/to/cloudflared
CHERT_VIEWER_SESSIONS=1
CHERT_FILES_URL_FILE=private/file-viewer-public-url
CHERT_LOGIN_URL_FILE=private/file-viewer-url
CHERT_VIEWER_SESSION_KEY=private/viewer-session.key
```

Leave `CHERT_FILES_URL` blank. Create the session key once as exactly 32 random
bytes in a private file with mode 0600. Never print it or put it in Git. Retain
it across ordinary restarts; replacing it invalidates existing browser sessions.
Render and install the four optional systemd templates, substituting `@USER@`,
`@APP@`, and `@VENV@` for the user, checkout, and virtual environment:

- `systemd/templates/chert-files.service.in`: runs the private login gateway
  on `127.0.0.1:8900`. Only the email-protected tunnel may expose this port.
- `systemd/templates/chert-files-tunnel.service.in`: runs `python -m chert.tunnel`,
  requiring `CHERT_TUNNEL_EMAIL`. The command always includes `--allowed-mail`;
  it records the actual allowed email and login URL in a private policy file.
- `systemd/templates/chert-viewer.service.in`: serves the authenticated viewer
  on `127.0.0.1:8902`. Every file, API, image, and static asset requires a signed
  session. Missing configuration fails closed. Only GET/HEAD are supported.
- `systemd/templates/chert-viewer-tunnel.service.in`: runs `python -m chert.tunnel
  --viewer` to expose port 8902. It refuses startup unless the origin rejects
  anonymous file API requests with the expected authentication-required marker.
  This tunnel delegates authentication to Chert; it never exposes port 8900.

After installation, run `sudo systemctl daemon-reload` and
`sudo systemctl enable --now chert-files chert-files-tunnel chert-viewer
chert-viewer-tunnel`. Read the shareable link from `private/file-viewer-public-url`.
Keep both HTTP listeners on loopback. The private login gateway is trusted only
because it sits behind cloudflared's single-email check; it must never be directly
published or used as the ordinary viewer tunnel's origin.

The public browser first gets a random HttpOnly login cookie. A signed state
carries only its hash to the protected login gateway. After Cloudflare checks
the allowed email, that gateway issues a 60-second grant bound to the initiating
browser. The public callback checks that binding, clears the login cookie, and
sets a Secure, HttpOnly, SameSite=Lax session cookie. Grants cannot be redeemed
without the initiating browser's cookie; public routes cannot issue grants or
trust user-supplied identity headers. Login redirects are not cached, send no
referrer, and remove the grant from the address bar immediately. Keep access
logging disabled for authentication URLs. `/logout` clears the browser session.

The runner publishes its current URL atomically and removes it when the tunnel
exits. Services restart after failures and reboots. **The public Quick Tunnel
hostname changes after its restart, so previously posted links expire.** Subsequent prompts
receive the new URL automatically. Cloudflare provides no uptime guarantee for
Quick Tunnels; configure a named tunnel and `CHERT_FILES_URL` for a stable domain.

When a Chert browser session expires, file API requests return JSON 401 and the
page offers **Sign in again**, preserving the document URL. Connection failures
have a separate retry action. For older installations, leaving
`CHERT_VIEWER_SESSIONS=0` retains the original protected viewer on port 8900;
that mode remains subject to Cloudflare's session-expiry issue.

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

`python tests/browser_viewer_auth.py` checks the complete browser handoff between
two temporary origins with a simulated five-second email cookie. It then verifies
folder navigation, document reads, auto-refresh, and reload after that cookie has
expired, plus anonymous denial and logout. Unit tests cover grant theft/replay,
tampering, cookie expiration, policy mismatch, key rotation, and startup refusal
if an origin is not authenticated. These tests do not perform a real email login.

Before exposing project files, test the tunnel against an empty origin and
verify its unauthenticated URL reaches Cloudflare's email sign-in form. Verify
both `/files/` and `/files/api/projects` are protected. The intended user must
complete the email sign-in; do not retrieve or share their login code.
