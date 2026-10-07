# Upstream integration and verification

Source: [ceselder/chert at 0bd0902](https://github.com/ceselder/chert/tree/0bd0902468437823863c2a38ddd5c7e01c9a61bd), audited on 2026-10-02.

The audited bridge and transcript parser now live in `chert/vendor/bridge.py` and
`chert/vendor/checkin.py`. Their contents are unchanged and pinned by hash tests.
The companion disk/S3 implementation is in `chert/vendor/storage.py`. A single
vendor boundary adapts historical imports, state paths, backup inventory, static
assets, and templates to the deployment root.

See [architecture](architecture.md) for application ownership and composition.
The upstream implementation is the reference for supported Claude behavior and
shared rendering; the current product intentionally uses project channels.

## Current interface

| Operation | Implementation |
| --- | --- |
| `/project name dir` | Registers an existing host directory and creates its channel |
| `/harness [name]` | Changes the durable default for new sessions; existing threads retain their harness |
| `/archive`, `/unarchive` | Moves the project channel between `projects` and `archived` |
| Plain prompts | Opens a message-attached thread in the current project directory |
| Voice messages/audio uploads | OpenAI speech transcription; recognized text is posted in the session thread before prompt delivery |
| `/codex`, `/claude`, `/astra` | Explicit harness launch in the current project; Astra is a Codex alias |
| `/sessions` | Lists both harnesses for the current project |
| `/resume`, `/fork` | Native history reuse/copy or cross-harness handoff |
| `/model`, `/effort`, `/fast`, `/mode`, `/yolo` | Native settings, with backend-specific capabilities |
| `/globalmodel` | Explicit harness-wide model default; owner-only |
| `/stop`, `/kill`, `/restart`, `/refresh`, `/revive` | Session lifecycle controls |
| `/screen`, `/key`, `/log` | Native terminal attachment/control and stored history |
| `/mute`, `/unmute`, `/supernova` | Session output and time-budget controls |
| `/disk`, `/backup`, `/s3`, `/offload`, `/restore` | Upstream disk/S3 tools, with both agent homes protected |
| `/feldspar` | Upstream two-engine review; requires the corresponding accounts |

`/all`, `/hub`, `/restartall`, `/reviveall`, and `/cleanup` are deliberately removed.
No harness-specific main, broadcast, shared-chat, or board channels are created.
The old standalone and compatibility frontends are gone. Project creation from
setup and from Discord uses one implementation.

## Preserved behavior

- One Discord connection and command registry; owner/allowlist authorization.
- Project directory matching for discovery, with the most specific match winning.
- Per-session webhook identities, upstream message formatting, attachments, and
  `hearth-send` file delivery.
  The frontend replaces case-insensitive `discord` with `chat` in outgoing
  webhook usernames to prevent Discord's reserved-name HTTP 400 errors. Session
  titles and avatar seeds are preserved. Regression tests exercise the outgoing
  webhook payload; they do not send test messages to production Discord.
- Immediate activity cards, tool counts, heartbeat updates, completion timing,
  compaction notices, and reconnect recovery. Raw reasoning deltas are not posted.
- Codex thread prefixes follow activity: 🔭 working/exploring, 📡 waiting for
  approval/input, 💤 idle, ⏹ interrupted, ❌ failed, ⚪ disconnected, 🌌 ended.
  Names and collision suffixes are preserved. Background renames coalesce to the
  latest state; Discord's per-thread rename rate limits can delay the visible emoji.
  Existing titles refresh from the last known state even when native history
  errors prevent attachment; such errors still limit activity reconciliation.
- Native approval/input controls. Approval identities are connection-scoped and
  old controls cannot approve a newer request.
- A parameterless `/model` picker; selected model/effort settings persist until
  the next accepted turn. Steering active work does not consume pending settings.
- Native terminal clients attach to the same Codex actor instead of creating a
  second conversation.
- Existing state schemas, native conversation identities, atomic writes, and
  last-good Codex state backups. Old `exec` records attach without replaying turns.
- Dashboard URLs, authenticated local control endpoints, and deployment entrypoints.

## Verification and limits

The test suite exercises the production frontend and composed harness components:
project routing, command schema, permissions, persistence, channel creation,
archiving, per-thread harness ownership, discovery, recovery, activity rendering,
model choices, approval/input controls, dashboard routes/assets, backup paths,
and HTTP authorization. Upstream contract tests detect changes to audited source.
Thread emoji tests cover lifecycle transitions, muted sessions, failed rename
recovery, and completion/ending while an older rename is blocked.

`tests/live_codex_activity.py` uses a separate temporary Codex home and app-server,
with an in-memory Discord sink. Its checks cover real inference/tool activity,
heartbeat and completion, the model picker followed by actual model switching,
and project cwd/thread routing after changing the default harness. These checks
never touch production Discord or run prompts in existing user conversations.
The activity check also asserts working and idle thread titles against the
in-memory Discord sink; it does not establish real Discord rename latency.

Claude remains intentionally disabled in production unless explicitly enabled.
Claude routing/adoption tests do not establish live Claude inference. S3 tests use
a fake client and do not establish production bucket access. Backend-specific
models, permissions, reasoning settings, and terminal menus are not claimed to be
identical across Codex and Claude.

Audio is a fork extension. Tests exercise real Ogg/Opus decoding, multipart HTTP
requests against a local test server, transcript publication before prompt delivery,
new and existing thread routing, mixed attachments, authorization, long transcripts,
and failure handling. These tests do not establish live OpenAI recognition accuracy
or end-to-end Discord audio delivery. Production speech recognition requires a
separately configured `OPENAI_API_KEY`; Codex subscription authentication is not used.

Deployment checks additionally verify the exact revision, gateway connection,
service stability, HTTP health, command schema, existing Discord resources,
state mappings, and the unchanged shared Codex daemon. Passing a unit test or
registering a command alone is not proof of live behavior.
