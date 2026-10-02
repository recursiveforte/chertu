<p align="center"><img src="static/chert.svg" width="240" alt="Chert, the little astronaut"></p>

# chert 🔭 — one Discord frontend, two coding backends

This fork of [ceselder/chert](https://github.com/ceselder/chert) uses **upstream Chert as its source of truth**. One bot, one command registry, and the upstream presentation serve two channels:

- **`#codex`** → Codex's native app-server.
- **`#claude`** → the original Claude Code integration.

Type a prompt in either channel. The bot adds 🚀/📡 reactions and opens a thread on your message. Reply in that thread to continue. A leading project directory selects that project: `my-project fix the tests`. Mentions and slash commands also work.

The [parity audit](docs/upstream-parity.md) inventories every upstream command, its backend mapping, verified behavior and limitations. The original [Claude reference](docs/claude-backend.md) is retained. `discord_bot.py` and `app.py` are the pinned upstream implementations, not independently rewritten copies.

## Shared behavior

- Automatic session discovery, attached threads, per-session webhook identities and avatars.
- The upstream command surface in both channels: history/search/resume, forks, rename, model/effort/fast, permission modes, stop/restart/revive, mute, countdowns and fleet controls.
- Approval buttons and user-input forms. Codex uses native approval responses; Claude retains upstream's terminal menus.
- Uploads, opened images, `hearth-send`, pinned boards, ask-all summaries and shared chat.
- Durable session mappings, backup recovery and native reboot revival.
- The upstream Signalscope dashboard for both backends, plus shared disk/S3 tools.

`/screen` and `/key` work for both backends. For Codex, Chert opens a terminal client connected to the same daemon conversation on demand; it does not start a second model session. Native approval buttons and input forms also work without opening a terminal.

## Install

Use Linux with systemd, Python 3.11+, tmux, and a Discord server where you can add a bot. Install the [Codex CLI](https://developers.openai.com/codex/cli/) and log in as the service user. The app-server interface is verified with version 0.160.0.

```bash
npm install -g @openai/codex
codex login                 # or: codex login --device-auth
```

Install and log into Claude Code if you want to use it. If you have no Claude account, set `CLAUDE_ENABLED=0` in `.env`: the channel stays configured and the bot explains that it is unavailable without launching a login screen.

Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications), enable **Message Content Intent**, then:

```bash
git clone https://github.com/recursiveforte/chert.git
cd chert
./setup.sh --backend both
```

Setup prompts privately for the token, prints the invitation link, and provisions a private `chert` category. It creates `#codex`, `#claude`, and the upstream-style auxiliary channels `#all-codex`, `#codex-chat`, `#all-claudes`, and `#claude-chat`.

Put projects beneath `~/projects`, or set `PROJECT_ROOT`. Both backends use upstream's project resolver: a leading existing project directory or explicit path selects that directory; otherwise the root is used. Discovered sessions keep their original working directory.

Services:

- `chert-discord-bridge`: the shared bot.
- `chert-codex-daemon`: idempotently ensures Codex's shared daemon is running, in a separate service from the bot.
- `chert-tmux`: Claude terminals and on-demand Codex terminal clients.
- `chert-checkin`: the local dashboard on `127.0.0.1:8899`.

Updating/restarting the bot does not restart the shared Codex daemon. Existing terminal/editor sessions continue running.

`--skip-discord` stages installation without provisioning channels or starting services. `--no-systemd` prepares manual execution with `.venv/bin/python chert.py`; start the Codex daemon yourself with `codex app-server daemon start`. `--dry-run` changes nothing.

## Commands

The primary command names, arguments, defaults and permission metadata come from upstream. They operate on the backend selected by the channel/thread:

| Operation | Commands |
| --- | --- |
| Launch | Plain prompt, `/codex`, `/claude`; `/astra` remains a Codex alias |
| Find / copy | `/sessions`, `/resume`, `/fork` |
| Configure | `/model`, `/globalmodel`, `/effort`, `/fast`, `/mode`, `/yolo`, `/rename` |
| Control | `/stop`, `/refresh`, `/restart`, `/revive`, `/kill` |
| Inspect | `/log`, `/screen`, `/key`, `/help` |
| Notifications / budget | `/mute`, `/unmute`, `/supernova` |
| Fleet | `/all`, `/restartall`, `/reviveall`, `/cleanup`, `/hub` |
| Host / reviews | `/disk`, `/backup`, `/s3`, `/offload`, `/restore`, `/feldspar` |

The `!` forms are also available. `/fork` defaults to the same backend; selecting the other backend performs a conversation handoff. `/model`, `/globalmodel`, and `/fast` remain owner-only.

A message in `#all-codex` or `#all-claudes` asks every session of that backend and collects replies for its persistent summarizer. `!all` broadcasts without summarization. The `*-chat` channels mirror their shared JSONL chat buses.

## Configuration

See [.env.example](.env.example), and [the upstream settings reference](docs/claude.env.example) for the original optional services.

| Setting | Purpose |
| --- | --- |
| `CHERT_BACKEND=both` | Shared frontend with both channels; `codex` supports a Codex-only shared frontend. `claude` retains the original standalone entrypoint. |
| `DISCORD_CODEX_CHANNEL_ID`, `DISCORD_CLAUDE_CHANNEL_ID` | Primary channels; setup fills them in. |
| `DISCORD_OWNER_ID`, `SPAWN_ALLOW_USERS` | Owner and additional permitted users. Both channels use the same policy. |
| `CLAUDE_ENABLED` | Set `0` until Claude is installed and authenticated. |
| `PROJECT_ROOT` | Project root for new sessions; default `~/projects`. |
| `CODEX_BIN` | Codex executable; setup stores its absolute path. |
| `CODEX_MODEL`, `CODEX_EFFORT` | Defaults for new Codex sessions; blank inherits Codex configuration. |
| `CODEX_APP_SERVER_SOCKET` | Defaults to `$CODEX_HOME/app-server-control/app-server-control.sock`. |
| `CODEX_DISCOVER`, `CODEX_DISCOVERY_INTERVAL` | Discovery enabled by default, every five seconds. |
| `CODEX_SANDBOX`, `CODEX_NETWORK_ACCESS` | Permissions for newly created Codex sessions. Existing sessions retain their configuration. |
| `CODEX_STATE_FILE` | Default `private/codex-state.json`; Claude state remains `bot_state.json`. |
| `CODEX_CHAT_LOG` | Default `~/shared/codex_chat/msgs.jsonl`. |
| `CODEX_PROMPT_CHANNEL_ID`, `CODEX_PROMPT_TARGET` | Optional AGENTS.md attachment sync, matching upstream's CLAUDE.md workflow. |
| `S3_BUCKET` and AWS configuration | Enable upstream Ash Twin backup/offload/restore for both backends. |

The bot suppresses model-generated mentions. New categories are private; Discord administrators retain access. Existing channel permissions are preserved. Adding an allowed user also requires granting channel access in Discord.

Daemon-based Codex controls use [native thread and turn APIs](https://learn.chatgpt.com/docs/app-server). Command/file/permission approvals and user-input/MCP forms appear in Discord. Approval buttons expire when their native request or connection expires; they never silently approve a newer request. Unsupported custom requests remain available in the original client.

The dashboard reuses the upstream templates at `/claudes/` and `/codex/`. It remains on loopback and retains upstream's authentication-proxy deployment model. Codex's conversation pane uses normalized live transcript records; its screen pane attaches a real terminal. `/log` and `/resume` access native stored history.

## Operate and test

```bash
journalctl -u chert-discord-bridge -f
sudo systemctl restart chert-discord-bridge
.venv/bin/python -m unittest discover -s tests -v
```

Back up the state files together with the corresponding agent homes. `.env`, private logs, session data and credentials are gitignored. S3 features require an explicitly configured private bucket; offload retains upstream's verification/confirmation flow.

For an existing Codex-only installation, run `./setup.sh --backend both`. It preserves `#codex`, its threads, webhook, and state, and adds `#claude`. You do not need a Claude account to keep using Codex.

## License

MIT; see [LICENSE](LICENSE) and [NOTICE](NOTICE). Original project and artwork by ceselder. Chert is a character from *Outer Wilds* by Mobius Digital. This fan project is not affiliated with Mobius Digital, Anthropic, or OpenAI.
