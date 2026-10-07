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
| `/stop`, `/close`, `/kill`, `/restart`, `/refresh`, `/revive` | Session lifecycle controls; `/close` aliases `/kill how:end` |
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
  Codex image inspection does not automatically upload the viewed file to Discord;
  outgoing files use explicit delivery. Regression tests verify that `imageView`
  events do not echo uploads or other local images and that explicit image delivery
  still targets the session. Discord sends are mocked; this has not been exercised
  end to end in production Discord.
  The frontend replaces case-insensitive `discord` with `chat` in outgoing
  webhook usernames to prevent Discord's reserved-name HTTP 400 errors. Session
  titles and avatar seeds are preserved. Regression tests exercise the outgoing
  webhook payload; they do not send test messages to production Discord.
- Immediate activity cards, tool counts, heartbeat updates, completion timing,
  compaction notices, and reconnect recovery. Raw reasoning deltas are not posted.
- Codex thread titles keep the fixed 🚀 prefix; activity changes appear in the
  activity card without renaming the thread. Explicit session names and collision
  suffixes still synchronize, and closing a session uses 🌌. Discovery restores
  earlier activity-based prefixes to 🚀, subject to Discord's rename rate limits.
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
Thread title tests verify that activity transitions do not trigger renames,
legacy prefixes are restored, failed renames retry, and closing supersedes
a pending name update.
`/close` tests exercise authorization, backend routing (including disabled Claude),
archived projects, and ending only the selected Codex session while archiving its
Discord thread and preserving its mapping. Codex closure archives Discord before
native cleanup, never resumes history to close, reports native cleanup failures,
and sends an ephemeral command response so it cannot reopen the thread. The ended
title is queued separately so rename throttling cannot delay archiving. Tests also
cover broken history, native cleanup errors, Discord permission errors/retries,
missing native IDs, and blocked renames. Codex RPC and Discord calls are mocked;
these checks do not establish live thread closure in production Discord.

Closed Codex sessions are excluded from activity cards and subagent refreshes,
including refreshes queued before closure. Closure waits for in-flight discovery,
event, and card writes before archiving. Regression tests reproduce the old
heartbeat-driven reopening and verify repeated ticks, persisted closed state
after restart, and a status write racing with closure using in-memory Discord.

`tests/live_codex_activity.py` uses a separate temporary Codex home and app-server,
with an in-memory Discord sink. Its checks cover real inference/tool activity,
heartbeat and completion, the model picker followed by actual model switching,
and project cwd/thread routing after changing the default harness. These checks
never touch production Discord or run prompts in existing user conversations.
The `--close` check exercises native archiving without attaching history, with an
in-memory Discord archive call. This check passed on 2026-10-06: the isolated
native actor unloaded and the Discord archive payload was verified. It does not
establish live closure of a production Discord thread.
On 2026-10-07, the extended check also passed repeated activity/discovery ticks
after closure without accessing the Discord channel again (in-memory sink).

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

On 2026-10-06, live `gpt-transcribe` recognition succeeded using the configured
production API key and OpenAI Whisper's public JFK speech fixture, converted to
Discord-style Ogg/Opus and processed through Chert's decoder and transcription
client in a temporary checkout. Transcript publication and prompt inclusion were
also checked with an in-memory Discord sink. The bridge was activated with the
key; no test audio or prompts were sent to production Discord conversations.

Deployment checks additionally verify the exact revision, gateway connection,
service stability, HTTP health, command schema, existing Discord resources,
state mappings, and the unchanged shared Codex daemon. Passing a unit test or
registering a command alone is not proof of live behavior.
