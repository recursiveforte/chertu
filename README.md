<p align="center"><img src="static/chert.svg" width="240" alt="Chert, the little astronaut"></p>

# chert 🔭 — one Discord frontend, two coding backends

Discord voice messages and audio uploads can be used as prompts in project channels
or existing session threads. Chert posts the recognized text in the session thread
before delivering it to the agent, alongside any typed caption and other attachments.
Transcripts are attributed to the sender, split across Discord messages when needed,
and cannot trigger mentions or bot commands.

Configure `OPENAI_API_KEY` in the private `.env` and restart the bridge to enable
speech recognition. The default is OpenAI's high-accuracy
[`gpt-transcribe`](https://developers.openai.com/api/docs/models/gpt-transcribe);
`AUDIO_TRANSCRIPTION_MODEL` can override it. API billing is separate from Codex login.
Recordings are sent to OpenAI for transcription. PyAV (included in requirements)
decodes Discord Ogg/Opus and common audio formats in a separate worker, with no
system FFmpeg installation required. Limits are 25 MB and 10 minutes per recording.
Temporary audio is deleted after processing; transcripts remain in Discord and the
agent conversation. A failed recording stops the entire message from being delivered
and produces an error so the sender can retry.

This fork of [ceselder/chert](https://github.com/ceselder/chert) uses **upstream Chert as its source of truth**. One bot and one command registry serve **one channel per project**. Each project binds a directory on the bot host to a Discord channel and has a default harness: Codex or Claude.

Create a project with `/project name:chert dir:/home/cheru/Code/chert`. Type a prompt in `#chert` to open a session thread using that directory and the project's default harness. Reply in the thread to continue. `/harness` opens a picker; `/harness name:claude` changes the default directly. Existing threads keep their original harness. `/codex` and `/claude` explicitly launch a session using that harness in the current project.

`/archive` moves the project channel, including its history and threads, into **archived**. Archived projects reject new prompts; already-running agents can finish. `/unarchive` moves it back into **projects**. Both commands accept an optional project name. Project bindings and harness choices survive restarts.

The [parity audit](docs/upstream-parity.md) inventories every upstream command, its backend mapping, verified behavior and limitations. The original [Claude reference](docs/claude-backend.md) is retained. `chert/vendor/bridge.py` and `chert/vendor/checkin.py` are the pinned upstream implementations. The application has one project frontend and composes independent harness services. See the [architecture guide](docs/architecture.md).

## Shared behavior

- Automatic session discovery, attached threads, per-session webhook identities and avatars.
- The upstream session controls: history/search/resume, forks, rename, model/effort/fast, permission modes, stop/restart/revive, mute and countdowns.
- Approval buttons and user-input forms. Codex uses native approval responses; Claude retains upstream's terminal menus.
- Uploads, opened images, and `hearth-send` file delivery.
- Durable session mappings, backup recovery and native reboot revival.
- The upstream Signalscope dashboard for both backends, plus shared disk/S3 tools.

`/screen` and `/key` work for both backends. For Codex, Chert opens a terminal client connected to the same daemon conversation on demand; it does not start a second model session. Native approval buttons and input forms also work without opening a terminal.

## Install

Use Linux with systemd, Python 3.11+, tmux, and a Discord server where you can add a bot. Install the [Codex CLI](https://developers.openai.com/codex/cli/) and log in as the service user. The app-server interface is verified with version 0.160.0.

```bash
npm install -g @openai/codex
codex login                 # or: codex login --device-auth
```

Install and log into Claude Code if you want to use it. If you have no Claude account, set `CLAUDE_ENABLED=0` in `.env`: projects remain available through Codex and the bot explains that Claude is unavailable.

Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications), enable **Message Content Intent**, then:

```bash
git clone https://github.com/recursiveforte/chert.git
cd chert
./setup.sh --backend both
```

Setup prompts privately for the token, prints the invitation link, and provisions private `projects` and `archived` categories. The initial `#chert` project uses the installation directory. Add more projects with `/project`; no harness-specific or broadcast/chat channels are created.

The `/project` directory must already exist on the bot host. Absolute paths and `~` work; relative paths resolve beneath `PROJECT_ROOT` (default `~/projects`). Prompts are passed intact, without treating their first word as a directory. Discovered sessions are placed in the project whose directory most closely contains their working directory; sessions outside registered projects are ignored.

Services:

- `chert-discord-bridge`: the shared bot.
- `chert-codex-daemon`: idempotently ensures Codex's shared daemon is running, in a separate service from the bot.
- `chert-tmux`: Claude terminals and on-demand Codex terminal clients.
- `chert-checkin`: the local dashboard on `127.0.0.1:8899`.

Updating/restarting the bot does not restart the shared Codex daemon. Existing terminal/editor sessions continue running.

`--skip-discord` stages installation without provisioning channels or starting services. `--no-systemd` prepares manual execution with `.venv/bin/python chert.py`; start the Codex daemon yourself with `codex app-server daemon start`. `--dry-run` changes nothing.

## Commands

Session commands use the harness recorded for the thread. Project channels select the default for new sessions:

| Operation | Commands |
| --- | --- |
| Projects | `/project name dir`, `/harness [name]`, `/archive [name]`, `/unarchive [name]` |
| Launch | Plain prompt, `/codex`, `/claude`; `/astra` remains a Codex alias |
| Find / copy | `/sessions`, `/resume`, `/fork` |
| Configure | `/model`, `/globalmodel`, `/effort`, `/fast`, `/mode`, `/yolo`, `/rename` |
| Control | `/stop`, `/close`, `/refresh`, `/restart`, `/revive`, `/kill` |
| Inspect | `/log`, `/screen`, `/key`, `/help` |
| Notifications / budget | `/mute`, `/unmute`, `/supernova` |
| Host / reviews | `/disk`, `/backup`, `/s3`, `/offload`, `/restore`, `/feldspar` |

The `!` forms are also available for the original commands. `/fork` defaults to the same backend; selecting the other backend performs a conversation handoff. `/model`, `/globalmodel`, and `/fast` remain owner-only.

Use `/close` inside a session thread to end its session and archive the thread,
keeping its history. This is a shortcut for `/kill how:end`; `/stop` only interrupts
the current turn. Use `/revive` or `/resume` to continue an ended session.

In a session thread, `/model` opens a picker showing the current and queued model.
You can also use `/model name:…` directly. In Codex, the selection applies to the next turn. It
does not interrupt a running turn; messages sent during that turn still steer the
current model. The choice remains queued until Codex accepts a new turn. `/effort`
works the same way. `/globalmodel` also changes the default for new sessions.

Harness-wide broadcast, hub and fleet commands are removed from the project interface.

## Configuration

See [.env.example](.env.example), and [the upstream settings reference](docs/claude.env.example) for the original optional services.

| Setting | Purpose |
| --- | --- |
| `CHERT_BACKEND=both` | Install both harness integrations. Project mode uses the shared frontend. |
| `DISCORD_GUILD_ID` | Server using project channels; setup fills it in. |
| `PROJECT_STATE_FILE` | Project/channel registry; default `private/projects.json`. |
| `DEFAULT_HARNESS` | Default for newly registered projects; `codex` unless configured otherwise. |
| `DISCORD_OWNER_ID`, `SPAWN_ALLOW_USERS` | Owner and additional permitted users. All project channels use the same policy. |
| `CLAUDE_ENABLED` | Set `0` until Claude is installed and authenticated. |
| `PROJECT_ROOT` | Project root for new sessions; default `~/projects`. |
| `CODEX_BIN` | Codex executable; setup stores its absolute path. |
| `CODEX_MODEL`, `CODEX_EFFORT` | Defaults for new Codex sessions; blank inherits Codex configuration. |
| `CODEX_APP_SERVER_SOCKET` | Defaults to `$CODEX_HOME/app-server-control/app-server-control.sock`. |
| `CODEX_DISCOVER`, `CODEX_DISCOVERY_INTERVAL` | Discovery enabled by default, every five seconds. |
| `CODEX_SANDBOX`, `CODEX_NETWORK_ACCESS` | Permissions for newly created Codex sessions. Existing sessions retain their configuration. |
| `CODEX_STATE_FILE` | Default `private/codex-state.json`; Claude state remains `bot_state.json`. |
| `S3_BUCKET` and AWS configuration | Enable upstream Ash Twin backup/offload/restore for both backends. |

The bot suppresses model-generated mentions. New categories are private; Discord administrators retain access. Existing channel permissions are preserved. Adding an allowed user also requires granting channel access in Discord.

Daemon-based Codex controls use [native thread and turn APIs](https://learn.chatgpt.com/docs/app-server). Command/file/permission approvals and user-input/MCP forms appear in Discord. Approval buttons expire when their native request or connection expires; they never silently approve a newer request. Unsupported custom requests remain available in the original client.

The dashboard reuses the upstream templates at `/claudes/` and `/codex/`. It remains on loopback and retains upstream's authentication-proxy deployment model. Codex's conversation pane uses normalized live transcript records; its screen pane attaches a real terminal. `/log` and `/resume` access native stored history.

## Code organization

Application code lives under `chert/`: `discord/` contains the gateway and UI, `backends/` contains the harnesses, `web/` contains the dashboard and local API, and `vendor/` isolates audited upstream code. Root Python files are thin deployment entrypoints. See [architecture](docs/architecture.md) for component boundaries and retained compatibility paths.

## Operate and test

```bash
journalctl -u chert-discord-bridge -f
sudo systemctl restart chert-discord-bridge
.venv/bin/python -m unittest discover -s tests -v
```

Back up the state files together with the corresponding agent homes. `.env`, private logs, session data and credentials are gitignored. S3 features require an explicitly configured private bucket; offload retains upstream's verification/confirmation flow.

For an existing installation, normal setup leaves existing channels alone. To replace **all channels** in a specific server with the project layout, stop the bridge first and run:

```bash
sudo systemctl stop chert-discord-bridge
.venv/bin/python setup_discord.py --guild cheru-land --reset-channels \
  --project chert /home/cheru/Code/chert
sudo systemctl start chert-discord-bridge
```

Use the actual project directory on your host; repeat `--project NAME DIR` to seed additional projects. The explicit reset permanently deletes Discord channels, messages and threads. It saves the old channel inventory and session mappings under `private/channel-resets/`; this is not a message backup. Native Codex/Claude histories remain on disk, and live sessions are rediscovered under their registered projects.

## License

MIT; see [LICENSE](LICENSE) and [NOTICE](NOTICE). Original project and artwork by ceselder. Chert is a character from *Outer Wilds* by Mobius Digital. This fan project is not affiliated with Mobius Digital, Anthropic, or OpenAI.
