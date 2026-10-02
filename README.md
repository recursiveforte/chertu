<p align="center"><img src="static/chert.svg" width="240" alt="Chert, the little astronaut"></p>

# chert 🔭 — Codex from Discord

This is [recursiveforte's fork](https://github.com/recursiveforte/chert) of
[ceselder/chert](https://github.com/ceselder/chert). **Codex is the default backend.**
Type a prompt in `#codex`, then reply in its Discord thread to continue the same
Codex conversation. No slash command or mention is needed. Existing Codex sessions
on the machine appear automatically. No Claude Code installation, hooks, or tmux are needed.

Chert runs the installed Codex CLI using [`codex exec --json` and explicit session
resume](https://developers.openai.com/codex/noninteractive/). It uses your normal
Codex login and configuration; it doesn't copy credentials into a separate Codex home.
For already-running sessions, Chert connects to the existing local
[Codex app-server](https://learn.chatgpt.com/docs/app-server) and shares the same live
conversation with your terminal or editor.

## What works

- A plain prompt in `#codex` creates a Discord thread and a Codex session.
  `/codex prompt [project]` is an optional way to choose a project.
- The thread is attached directly to your prompt, like upstream Chert, with 🚀/📡
  reactions and a `🚀 prompt-slug` title. There is no separate “Continue in…” post
  in the channel. Replies and activity updates use the original project/session
  webhook identity and a stable robot avatar; turn recaps include elapsed time.
- Like upstream, a leading project directory name selects that project:
  `my-project fix the failing test`. Otherwise the prompt runs in `PROJECT_ROOT`.
- Existing user-facing sessions loaded in the local Codex daemon get threads
  automatically, including sessions already running when the bridge starts.
  Their new assistant messages are mirrored into Discord. Internal subagents,
  ephemeral jobs, and old unloaded history don't create extra threads.
- Replies in discovered threads go directly to the original session. If it is busy,
  the reply steers its active turn; if idle, it starts a new turn in the same session.
  Chert doesn't start another process against the live conversation.
- Messages in a bot-created thread resume its saved Codex session ID. Replies received while
  working are queued and processed in order, one turn at a time per thread.
- Progress messages show tool activity; completed answers are posted in the thread.
  Very long answers are attached as a text file.
- `/stop` interrupts the turn and its subprocesses and clears queued messages.
- `/sessions`, `/resume`, `/rename`, `/model`, `/effort`, `/kill`, and `/help` manage sessions.
- Session IDs, project paths, model choices, and thread mappings survive bridge restarts.
  Interrupted turns and queued messages are **not automatically replayed**. Send a
  new message to continue; this avoids repeating operations that already changed files.
- Every command and plain reply is checked against the owner/allowed-user list.
  New installations create a private `chert` category with `#codex`.

The original Claude backend is still available with `./setup.sh --backend claude`.
Its commands, dashboard, hooks, and optional Astra integration are documented in
[the upstream guide](docs/claude-backend.md). The Codex backend doesn't include
Claude's broadcast summarizer, dashboard, S3 tools, or Feldspar reviewers.
Approval/input requests from discovered sessions stay in their original Codex
client; Chert notifies you to respond there.

## Install

Requirements: Python 3.11+, the [Codex CLI](https://developers.openai.com/codex/cli/),
and a Discord server where you can add a bot. Linux with systemd is the supported
service deployment; `--no-systemd` supports manual execution on Linux/macOS.

1. Install Codex and authenticate as the Linux user who will run the bridge:

   ```bash
   npm install -g @openai/codex
   codex login
   ```

   For a headless host, use `codex login --device-auth`. Ensure `codex login status`
   succeeds. Chert uses your Codex-configured model unless `CODEX_MODEL` is set.

2. Create an application at the [Discord Developer Portal](https://discord.com/developers/applications).
   On **Bot**, enable **Message Content Intent**, then copy the bot token.

3. Install this fork:

   ```bash
   git clone https://github.com/recursiveforte/chert.git
   cd chert
   ./setup.sh
   ```

   The installer prompts for the token without echoing it, prints an invite link,
   provisions the channel, and starts `chert-discord-bridge`. `.env` is private and
   gitignored. `--dry-run` changes nothing; `--skip-discord` stages an installation
   without provisioning channels or starting services.

4. Type a prompt in `#codex`, such as `Help me build a small website`. To select
   a particular project under `~/projects` (or `PROJECT_ROOT`), optionally use:

   ```text
   /codex prompt:Explain this project project:my-project
   ```

   The project must already exist beneath `PROJECT_ROOT`. Omit `project` to use the
   root directory. Absolute paths outside the root, `..` escapes, and symlink escapes
   are rejected.

For manual execution:

```bash
./setup.sh --no-systemd
.venv/bin/python chert.py
```

If Codex was installed somewhere outside the service's PATH, set its absolute path
in `CODEX_BIN`. Run `codex login` as the same user, with the same `CODEX_HOME` if set.

## Commands

| Command | Behavior |
| --- | --- |
| `/codex prompt [project]` | Start a session in a new Discord thread. |
| `/resume session [project]` | Attach a saved Codex UUID, or reopen its existing Chert thread. For an imported session, specify its project beneath `PROJECT_ROOT`. |
| `/sessions` | List sessions, status, completed turns, and Codex IDs. |
| `/stop` | Stop the active turn and clear its queue; the conversation remains resumable. |
| `/kill` | Stop and archive this session. `/resume` reopens it. |
| `/rename value` | Rename the current thread. |
| `/model value` | Select a model for subsequent turns; `default` restores the CLI's configured model. |
| `/effort value` | Select a model-supported reasoning effort; `default` restores the CLI setting. |
| `/help` | Show available commands. |

Text commands: `!codex <prompt>`, `!sessions`, `!stop`, `!kill`, `!rename <name>`,
`!model <name>`, `!effort <level>`, and `!help`. Plain messages in `#codex` start
new sessions attached to that message; mentions also work. A leading project directory
name selects a project, or use `/codex` to choose one explicitly. `/resume` attaches an ID.
Attachments aren't imported; put files in the project directory and refer to their paths.

## Automatic discovery

Chert polls the existing Codex app-server's loaded sessions every five seconds and
subscribes to their assistant messages. It remembers the Discord mapping across
restarts and does not repost the entire historical transcript. Discovered sessions
keep their original project directory, including directories outside `PROJECT_ROOT`.
That setting restricts project selection for new sessions, not existing conversations.

This requires a recent Codex daemon exposing its local control socket (verified
with app-server 0.160.0). By default Chert uses
`~/.codex/app-server-control/app-server-control.sock`, or the equivalent beneath
`CODEX_HOME`. Set `CODEX_APP_SERVER_SOCKET` for another local daemon. Chert never
starts or restarts your daemon. If it isn't running, discovery retries in the
background while new Discord-created sessions still work through `codex exec`.
Older standalone sessions without an app-server connection aren't live-discovered;
after closing them, `/resume` can attach their stored session IDs.

`/stop` interrupts the active turn of a discovered session. `/kill` interrupts it
and archives its Discord thread; it doesn't shut down the shared daemon or its
other sessions. A killed thread stays hidden until explicitly reopened with `/resume`.

## Configuration and access

See [.env.example](.env.example). Settings include:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CHERT_BACKEND` | `codex` | `codex` or `claude`. |
| `PROJECT_ROOT` | `~/projects` | Projects selectable from Discord. |
| `DISCORD_OWNER_ID` | Server owner | Can use all commands and session threads. |
| `SPAWN_ALLOW_USERS` | Owner only | Additional comma-separated user IDs permitted to control **all** Chert sessions. |
| `CODEX_MODEL`, `CODEX_EFFORT` | Codex configuration | Default model and reasoning effort for new sessions. |
| `CODEX_SANDBOX` | `workspace-write` | Codex command sandbox; can also be `read-only` or explicitly `danger-full-access`. |
| `CODEX_NETWORK_ACCESS` | `0` | Set to `1` to enable network access for workspace-write commands. |
| `CODEX_TURN_TIMEOUT` | `10800` | Maximum seconds per turn. |
| `CODEX_STATE_FILE` | `private/codex-state.json` | Persistent thread/session map. |
| `CODEX_LOG_DIR` | `private/codex-logs` | Private per-turn event, answer, and error logs. |
| `CODEX_DISCOVER` | `1` | Automatically discover loaded user-facing Codex sessions. |
| `CODEX_DISCOVERY_INTERVAL` | `5` | Seconds between discovery polls. |
| `CODEX_APP_SERVER_SOCKET` | Codex home control socket | Unix socket for the already-running Codex daemon. |

New bot-created Codex sessions run non-interactively with approvals set to `never`. An operation that needs
approval fails instead of displaying Discord approval buttons. Chert never retries
with sandboxing disabled. Sandbox startup errors remain errors until the host or
configuration is fixed. `PROJECT_ROOT` restricts project selection; Codex's sandbox
controls command access.
Discovered sessions keep the original client's sandbox and approval policies;
attaching Chert doesn't override them.

New categories deny access to `@everyone` and allow the configured owner, bot, and
explicit allowed users. Discord server administrators retain access. Existing
category/channel permissions are preserved; review them when reusing an installation.
When adding allowed users later, also grant them access to the category in Discord.
Bot replies suppress mentions, including model-generated `@everyone`.

## Operate and develop

```bash
journalctl -u chert-discord-bridge -f
sudo systemctl restart chert-discord-bridge
.venv/bin/python -m unittest discover -s tests -v
```

Keep `.env`, `private/`, and your Codex credentials out of Git. Back up the session
state together with your Codex home to retain resumable conversations. Local per-turn
logs contain conversation output; remove older files when no longer needed.
A second bridge using the same state file is rejected. On service restart, active
bot-created turn subprocesses are stopped by systemd and their sessions can be resumed.
Existing sessions in the separate Codex daemon keep running and are rediscovered.

To change an existing upstream installation, set `CHERT_BACKEND=codex`, install and
log into Codex, then run `./setup.sh --backend codex`. It provisions `#codex` and
updates the bridge entrypoint. The old Claude tmux/dashboard services are not needed
by Codex and can be disabled when you no longer use them. Legacy Claude state remains
in `bot_state.json`; Codex state is stored separately.

## License

MIT; see [LICENSE](LICENSE) and [NOTICE](NOTICE). Original project and artwork by
ceselder. Chert is a character from *Outer Wilds* by Mobius Digital. This fan project
is not affiliated with Mobius Digital, Anthropic, or OpenAI.
