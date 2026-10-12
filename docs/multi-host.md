# Multiple execution hosts

One Discord gateway can control Codex on several machines. The gateway can stay
on lemma while a MacBook provides its own files, tools, logged-in Codex runtime,
and desktop. No Discord bot token or second Chert service is needed on the Mac.

The built-in host `local` is the gateway machine. All existing project records and
sessions default to `local`. Remote hosts are opt-in. They do not share a filesystem
or Codex authentication with the gateway.

## Connect a MacBook

1. Enable macOS Remote Login for the intended account. From lemma, establish SSH
   key authentication and verify the Mac's host key. Use an SSH config alias if
   needed. `ssh -o BatchMode=yes cheru@macbook true` must succeed without a prompt.
   The address must remain reachable from lemma, for example on your private network.
2. On the Mac, install Python 3 and tmux, install/sign in to Codex, and open the
   desktop app. Use the existing user's daemon and socket. If using only the CLI,
   `codex app-server daemon start` starts its shared daemon. Do not launch a second
   app-server with a different home to access existing desktop conversations.
3. Copy `hosts.example.json` to `private/hosts.json` on lemma. Replace the SSH
   destination, home directory, and executable paths with the Mac's actual values.
   The socket must be an absolute path. Homebrew paths in the example are for an
   Apple Silicon installation; they are not checked or installed automatically.
4. Set `CHERT_HOSTS_FILE=private/hosts.json` in lemma's private `.env`, then restart
   only `chert-discord-bridge`. Host configuration is loaded at startup.
5. In Discord, use `/hosts`, then create a project:
   `/project name:desktop dir:/Users/cheru/Code host:macbook`.
   The directory is validated on the Mac. Relative paths resolve against that
   host's `project_root`; `~` refers to the remote account's home.

`hosts.example.json` is an example, not a configured connection. The helper uses
only Python's standard library and is sent over SSH for each filesystem or tmux
operation; installing Chert's Python dependencies on the Mac is unnecessary.
`path` optionally supplies the remote helper and terminal's PATH (useful for
Homebrew). `runtime` defaults to `~/.local/share/chert` on the remote account and
contains its attachments and managed worktrees.

The bridge forwards the private Codex Unix socket through SSH into a private
temporary directory on lemma. It does not expose Codex on a public TCP port or
disable SSH host-key verification. Allow Unix socket forwarding on the SSH server.
The SSH account must own or have access to the Codex socket.

## Computer use

On the Mac, install and enable the Computer Use plugin in the desktop app. Grant
Screen Recording and Accessibility permissions when prompted. These are separate
from SSH and must be configured in the logged-in desktop account. See the
[official Computer Use setup](https://learn.chatgpt.com/docs/computer-use).

Chert provides host routing; it does not provision desktop permissions or guarantee
that a standalone CLI daemon has the desktop app's tools. Verify computer use in
the Mac's desktop app first, register its project directory in Chert, and use the
discovered conversation from Discord. This preserves the native conversation's
settings and tools. Mac desktop control through Chert must be tested on the actual
Mac before considering it verified.

## Behavior

- Project channels select a host at creation. Create another project channel for
  another host, even if both machines use the same directory path. Existing
  session threads retain their recorded host. Host names are durable identifiers;
  do not rename them or repoint them to a different machine with active sessions.
- Discovery, prompts, approvals, model selection, history, reactions, lifecycle
  controls, terminals, and worktrees use the session's host. `/globalmodel`,
  `/fast everywhere`, and `/yolo` affect that host's Codex sessions.
- Uploads are copied to the selected host before the prompt is submitted.
  `/download path:…` sends a file from the current Codex session's host back to
  Discord. Relative paths start at the session's working directory. Remote
  transfers are limited to 25 MB. `hearth-send` remains gateway-local.
- Remote hosts support Codex. Claude, cross-harness handoffs, Feldspar, and the
  disk/S3 commands remain local; remote requests reject these operations.
  Remote native history is available through Codex; gateway S3 recovery does not
  restore remote hosts' histories. Back up each host's Codex home separately.
- A sleeping/offline Mac affects its own sessions. Prompts fail visibly; Chert
  does not resend ambiguous submissions or run them on lemma. Discovery retries
  the SSH connection when the host returns. Worktrees and uploads remain on the
  host for later review or cleanup.
- The shared dashboard includes all hosts. Remote links include both host and
  native conversation ID. Removing a host from configuration retains its saved
  sessions and produces an explicit error when controlling them.

The gateway retains `private/codex-state.json` as the authoritative mapping for
all hosts. Remote journals live in `private/hosts/<host>/codex-transcripts/`;
local journal locations are unchanged. Back up `private/` and `.env` before an
upgrade. Older releases may not understand the added host fields, so rollback
requires preserving and adapting newer state rather than replacing it blindly.
