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
| `/project name dir [host]`, `/hosts` | Registers a directory on a configured execution host; lists connection status |
| `/download path` | Sends a file from the Codex session's host to Discord |
| `/harness [name]` | Changes the durable default for new sessions; existing threads retain their harness |
| `/worktrees [enabled]` | Views/changes the durable per-project default for fresh Git worktrees, shown in the channel topic; defaults to off |
| `/worktree prompt`, `/no-worktree prompt` | Starts a new session with the project harness, overriding the workspace default once |
| `/archive`, `/unarchive` | Moves the project channel between `projects` and `archived` |
| Plain prompts | Opens a message-attached thread in the project directory or a fresh worktree, following its default |
| Codex prompt reactions | Native queue: ↪️ waiting, 👀 running, ✅ completed; ❌ failed/interrupted/removed |
| Click ↪️ on a queued Codex prompt | Authorized users send that prompt into the active turn; its reaction becomes 👀, then ✅ on completion |
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

Multi-host Codex routing is implemented over SSH, with separate connections,
host-scoped discovery, approvals, settings, uploads, worktrees, terminals, and
dashboard identities. Existing records default to the local host. Regression
tests exercise host isolation, matching native IDs, offline failure without local
fallback, remote filesystem operations, routing, and persistence. The MacBook
connection has been exercised live as described below; desktop computer use has
not been exercised. Remote Claude,
Feldspar, disk/S3 commands, S3 transcript recovery, and `hearth-send` are not
implemented; see [the exact scope and setup](multi-host.md).

On 2026-10-12, the isolated native `tests/live_codex_activity.py --projects`
check passed: a project prompt completed on the intended native directory and
Discord sink, and changing the default harness retained a working existing Codex
conversation. The test uses a private temporary CODEX_HOME and no production
Discord messages. Its in-memory channel lookup now covers thread-title updates.

The `macbook` host was subsequently enabled on lemma through authenticated SSH
to the Mac's existing Codex daemon. Live checks passed for Unix socket forwarding,
Codex initialization and model listing, remote directory validation, tmux 3.7c,
and an upload/download round trip whose temporary file was removed. MCP status
listed the Mac's `computer-use` and `cua_repl` integrations; this does not establish
that desktop permissions or an actual computer-use task work. No model prompt or
test thread was sent to production Discord or an existing native conversation.
After restarting only the bridge, both HTTP health checks returned 200, the
Discord gateway connected, and existing project bindings and conversation
mappings were preserved. No Mac project channel was created automatically.

On 2026-10-13, per-host `yolo` defaults were added for new Chert threads, and
`/mode` was corrected to update loaded native threads through
`thread/settings/update`; `thread/resume` ignores those overrides once loaded.
The isolated `tests/live_codex_activity.py --yolo` check verifies native YOLO
settings and actual tool execution. The four open production MacBook threads'
YOLO settings were read back successfully after updating them through the native
API. Settings changes apply to subsequent turns: an approval already pending in
an active turn is not automatically answered. No test prompts were sent to those
conversations or production Discord.

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
  Codex replies and bot-voice messages convert Markdown pipe tables to labeled
  rows before message splitting so long cells wrap in Discord. Headers, inline
  formatting, links, and cell contents are retained; code examples stay literal.
  Each cell uses an unindented labeled line, with blank lines between rows:
  Discord collapsed the former bullet continuation lines into adjacent cells.
  Regression checks cover the screenshot's milestone table, escaped/code pipes,
  and long replies through an in-memory webhook. Production Discord rendering has
  not been exercised by these tests; existing posted messages are unchanged.
  Codex replies send every numbered message instead of the upstream five-message
  cap and dashboard-only truncation notice. Regression tests reconstruct a reply
  spanning more than ten messages and verify table rows beyond the old cap,
  message length limits, and short replies through mocked Discord sends. This
  behavior has not been exercised end to end in production Discord.
- Immediate activity cards, tool counts, heartbeat updates, completion timing,
  compaction notices, and reconnect recovery. Raw reasoning deltas are not posted.
  The first turn edits the launch/discovery card in place so it does not leave
  a stale “ready” card above “working”. Later turns retain separate cards.
  Regression tests cover initial cards before and after a status refresh, plus
  completion and subsequent turns; this transition has not been exercised live.
- Codex status cards show session/workspace identity, a relative update timestamp,
  model, and dashboard/reply/screen/help shortcuts. Waiting for approval or input
  reads “🔔 needs you”. Tool progress and frozen completion timing are retained.
  Completed turns collapse to one small line with elapsed time and tool counts,
  removing the repeated status, session metadata, timestamp, and shortcuts.
  Regression checks cover the compact completion and frozen timing; this change
  has not been visually verified in production Discord.
  The same layout is used for launch and discovery. Regression checks use an
  in-memory Discord sink; the screenshot style has not been visually verified
  in production Discord. Codex does not claim Claude classifier rerouting.
- Codex thread titles use 🚀, switching to 💤 after 36 hours without interaction.
  New prompt-based names preserve readable wording, capitalization, punctuation,
  and Unicode instead of using a 32-character slug. Whitespace is collapsed and
  long names end at a word boundary with an ellipsis within Discord's title limit.
  Existing native names are preserved. Formatting is verified locally; new-thread
  creation has not been exercised live in production Discord for this change.
  Accepted prompts, thread commands, and native turn activity refresh a persisted
  clock; polling and card refreshes do not. Running turns stay 🚀. Interaction
  restores 🚀, and closing uses 🌌. Names and collision suffixes are preserved.
  Maintenance checks every minute, even when native discovery is unavailable;
  Discord rename rate limits may delay the visible change.
- Native approval/input controls. Approval identities are connection-scoped and
  old controls cannot approve a newer request.
  Fieldless MCP object forms offer Allow/Decline/Cancel when an empty response
  satisfies the schema, avoiding the raw JSON editor for confirmation prompts.
  Boolean form fields accept yes/no as well as JSON true/false, with schema
  validation retained. Repeated delivery of the same connection-scoped request
  posts once; distinct requests remain separate. Regression tests exercise clicks,
  permissions, validation, duplicate delivery, and retry after a failed Discord
  send using mocks. These changes have not been exercised end to end with a live
  Computer Use permission request in production Discord.
- A parameterless `/model` picker; selected model/effort settings persist until
  the next accepted prompt. Queued prompts apply pending settings through native
  `thread/settings/update`, without modifying the active turn.
- Discord Codex prompts use `thread/queue/add` for both idle and busy sessions.
  Codex owns storage, ordering, and automatic dispatch; Chert stores only Discord
  message/client IDs and reaction state. Native user-message events link dequeued
  prompts to turns, and native history repairs missed reaction updates after restart.
  Only the bot's lifecycle reactions are replaced. Failed/interrupted turns and
  removed queue entries never receive a success checkmark.
  Clicking ↪️ uses uncached Discord reaction events and the native queue's current
  content, including attachments. It removes the selected queue entry before
  steering the active turn; idle sessions use native `thread/queue/start` instead.
  Stale/repeated clicks cannot replay a consumed entry. A definite steer rejection
  returns the prompt to Codex's queue; ambiguous transport failures are reported
  without automatically resubmitting. The user's click reaction is removed too.
- Native terminal clients attach to the same Codex actor instead of creating a
  second conversation.
- Existing state schemas, native conversation identities, atomic writes, and
  last-good Codex state backups. Old `exec` records attach without replaying turns.
- Dashboard URLs, authenticated local control endpoints, and deployment entrypoints.

## Verification and limits

Per-project Codex fast defaults are a fork extension. Owner-only
`/fast-default mode:on|off|inherit` changes new sessions in the current project
channel, including worktrees; the parameterless command shows the setting.
Project on/off overrides the host default, and inherit retains the previous
host/Codex behavior. Settings survive restarts; existing threads, resumes, and
forks do not acquire the project override. Regression tests cover permissions,
thread-parent scoping, legacy state, persistence, rollback on write failure,
on/off/inherit precedence, worktrees, remote host isolation, native queue settings,
and preservation of existing sessions. On 2026-10-12,
`tests/live_codex_activity.py --fast-default` passed against an isolated native
Codex runtime: fast and default service tiers were accepted and completed real
inference, and changing the project default did not send new settings to an
existing thread. Discord was an in-memory sink; this does not establish live
production Discord command invocation or a latency improvement.

Per-project worktrees are a fork extension. Regression checks use real temporary
Git repositories to verify unique branches, concurrent isolation, dirty source
preservation, subdirectory projects, non-Git errors, persistence, and project
routing after restart. Discord/Codex mocks cover launch cwd, command permissions,
overrides, unchanged existing sessions, and disabled/enabled Claude routing.
Worktrees and their branches are retained on close and launch failure. History
resume and conversation forks retain their existing workspace; the default applies
to fresh sessions. Production Discord worktree launches have not been exercised
by these tests. On 2026-10-07, `tests/live_codex_activity.py --worktrees` passed
against an isolated native Codex runtime: a plain prompt used the enabled project
default, both slash overrides selected the correct cwd and completed inference,
and an existing worktree session continued after the project harness changed.
Discord was an in-memory sink; the production daemon and conversations were untouched.

The test suite exercises the production frontend and composed harness components:
project routing, command schema, permissions, persistence, channel creation,
archiving, per-thread harness ownership, discovery, recovery, activity rendering,
model choices, approval/input controls, dashboard routes/assets, backup paths,
and HTTP authorization. Upstream contract tests detect changes to audited source.
Thread title tests verify the 36-hour boundary, waking on interaction, persisted
idle clocks, polling without resetting the timer, running/ended sessions, archived
threads, rename retries, and closing superseding a pending name update. These
clock-controlled tests do not establish a live 36-hour transition in Discord.
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
On 2026-10-07, `--queue` passed against the installed 0.161.0 daemon binary in an
isolated app-server: two follow-ups appeared in Codex's native queue, then ran as
separate turns in FIFO order after the initial turn. All three prompts reached ✅,
queued prompts transitioned ↪️ → 👀 → ✅, and a reconstructed bridge state recovered
completion from native history without resubmitting. Discord reaction calls used
an in-memory sink; this does not establish end-to-end production Discord delivery.
The `--queue-steer` check also passed on 2026-10-07: an authorized raw reaction
event selected the second waiting prompt, removed only that native queue entry,
and steered it into the existing turn. Its text appeared in that turn's response
and it reached ✅; the other prompt ran afterward, with no duplicate promoted turn.
Discord events and reaction calls were simulated; inference and queue operations
used the isolated real daemon. Unit tests additionally cover authorization,
concurrent clicks, missing queue entries, rejected/ambiguous delivery, and restart
reconciliation using the acknowledged turn ID when history omits a steering client ID.

Activity cards follow ordered native turn events. Queue acceptance and the socket
reader's newer active-turn cache do not advance the displayed turn. An `active`
thread status before the next `turn/started` cannot reopen the preceding completed
card. Discovery and history recovery defer while that conversation has pending
notifications, including a notification already taken by the consumer.
Regression tests reproduce stale working cards, premature turn switches, late
progress events, and discovery/history races. On 2026-10-07,
`tests/live_codex_activity.py --rapid-queue` passed with five rapid prompts in one
isolated native conversation, delayed in-memory Discord delivery, and concurrent
discovery: five FIFO replies, five finalized cards, no leftover working previews,
and five completion reactions. Production Discord delivery was not exercised.

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
