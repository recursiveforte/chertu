# Upstream parity audit

Source of truth: [ceselder/chert at 0bd0902](https://github.com/ceselder/chert/tree/0bd0902468437823863c2a38ddd5c7e01c9a61bd), the current upstream `main` when audited on 2026-10-02.

## Finding

The earlier Codex fork was **not functionally equivalent** to upstream. Its nine slash commands and separately implemented frontend omitted much of upstream's 34-command surface. Matching the appearance of a thread did not establish functional parity.

The primary entrypoint now uses **one Discord connection, one upstream-derived command registry, shared upstream renderers, and two channel adapters**. `#claude` delegates to the original implementation; `#codex` uses Codex's native app-server. Both accept plain prompts and retain upstream's message-attached thread presentation. Those plain prompts are an explicitly requested extension to upstream's mention-based launcher.

`discord_bot.py` and `app.py` remain byte-for-byte the audited upstream versions. Contract tests pin their hashes and compare every upstream slash command's parameters and permission metadata. The fork adds only `/codex` and `/stop` to that registry. Fork destination defaults to the current backend and offers Claude/Codex choices.

## Command inventory

All entries below are registered once and dispatched by channel. “Upstream” means the original implementation is called, not rewritten.

| Upstream command | Claude adapter | Codex adapter |
| --- | --- | --- |
| `claude` | Upstream launcher in #claude | Explicit launcher routes to #claude |
| `astra` | Alias routes to #codex | Native Codex launcher |
| `sessions` | Upstream live registry | Persistent Codex map and live daemon discovery |
| `resume` | Upstream history search, autocomplete, selection and restore | Native live/archived history search, autocomplete, same selection UI, S3 restore |
| `fork` | Upstream fork; handoff to #codex | Native history fork; handoff to #claude |
| `model` | Upstream, owner-only | Native next-turn model, owner-only |
| `globalmodel` | Upstream, owner-only | Existing Codex sessions plus new-session default, owner-only |
| `effort` | Upstream | Native reasoning effort |
| `fast` | Upstream, owner-only | Native service tier, per-session or backend-wide, owner-only |
| `mode` | Upstream permission-mode control | Native approvals/sandbox settings; plan collaboration mode |
| `yolo` | Upstream expiring override for new sessions | Persistent expiring override for new Codex sessions |
| `rename` | Upstream native rename and Discord title | Native thread/name/set and Discord title |
| `refresh` | Upstream unstick/restart logic | Interrupt/re-attach; forced refresh reloads the native thread actor |
| `restart` | Upstream process restart, history retained | Archive/unarchive/reload of the selected actor, history retained |
| `revive` | Upstream resume, force and fork modes | Native attach/unarchive/backup restore; force and fork modes |
| `kill` | Upstream termination/archive/delete | Interrupt and archive selected native thread; optional Discord deletion |
| `screen` | Upstream terminal capture | On-demand Codex TUI client attached to the same daemon conversation; real tmux capture |
| `key` | Upstream terminal input | Same key map/menu controls, delivered to the attached Codex terminal |
| `log` | Upstream transcript timeline | Native persisted turn/item history |
| `mute`, `unmute` | Upstream | Suppresses mirrored output while continuing to record history |
| `supernova` | Upstream persistent countdown | Persistent countdown, cancellation, warning, wrap/interrupt/end actions |
| `all` | Upstream fleet delivery | Fleet delivery to Codex sessions |
| `restartall` | Upstream | Reload idle Codex actors; force allows interruption |
| `reviveall` | Upstream | Restore ended/interrupted/disconnected Codex sessions |
| `cleanup` | Upstream | Archive/delete ended Discord threads |
| `disk` | Upstream shared disk tools | Same shared disk tools |
| `backup`, `s3` | Upstream Ash Twin | Same Ash Twin, with Codex transcripts/config/state added |
| `offload`, `restore` | Upstream verified offload/restore | Same implementation; both agent homes protected |
| `hub` | Upstream persistent summarizer | Persistent Codex summarizer |
| `feldspar` | Original two-engine reviewer | Same reviewer against the Codex session's project |
| `help` | Upstream guide | Generated from the shared registry, with native capability notes |

## Non-command behavior

| Behavior | Implementation |
| --- | --- |
| Prompt → attached thread, reactions, slug titles | Preserved in both channel launchers |
| Project selection | Both adapters use upstream's resolver, including explicit existing paths |
| Per-project/session webhook identity and robot avatar | Shared upstream renderers; existing webhook identity preserved during migration |
| Activity cards, model, elapsed time and tool counts | Codex events normalized for upstream `update_card` / `card_text` |
| Live session discovery | Claude's original registry/hooks; Codex daemon's loaded user-facing threads |
| Reply to active work | Original Claude delivery; Codex turn/steer with exact active turn ID |
| Approvals and input | Original Claude menu buttons; native Codex command/file/permission decisions, user-input and MCP forms/URL flows |
| Incoming attachments | Original upstream upload/path handling used by both adapters |
| Opened images and outbound files | Native image events plus shared `hearth-send` endpoint and session ID routing |
| Pinned board and presence | Per-backend boards; combined bot presence |
| Ask-all and shared chat | `#all-claudes` / `#claude-chat`; matching `#all-codex` / `#codex-chat` |
| Persistent state and recovery | Separate compatible state files; atomic saves and last-good backup recovery |
| Reboot/crash revival | Original settings applied to native Codex actor restoration; prompts are not silently replayed |
| Reconnect output recovery | Completed replies since the persisted checkpoint are recovered from native history without rerunning turns |
| Web dashboard | Same Signalscope templates and transcript renderer, `/claudes/` and `/codex/` routes |
| Prompt attachment sync | Original CLAUDE.md sync; optional AGENTS.md sync for Codex |
| Disk/S3 support | Original shared implementation, extended backup inventory and Codex transcript restore |

## Differences that must not be hidden

- **Terminal attachment differs internally.** Claude already runs inside its tmux pane. Codex creates a TUI client on demand with `--remote … resume <id>`; it attaches to the existing actor instead of starting another model session. Screen/menu contents are naturally Codex's, but the same frontend controls work. Pane ownership and start commands are checked before sending keys.
- **Backend policies and model capabilities differ.** Codex maps permission/plan/fast/effort controls to native concepts. This is not a claim that the two engines have identical models, pricing, policy decisions or runtime internals.
- **Discovery scope is native.** Codex discovers loaded user-facing app-server conversations, not every historical JSONL file or internal subagent. A standalone terminal that does not connect to the shared daemon must be closed and explicitly resumed, or connected through `codex --remote unix://`.
- **Approval IDs are connection-scoped.** Old Codex approval buttons expire across reconnects; they cannot approve a different request with a recycled ID. The current native request must be answered. No automatic approval is sent.
- **Access policy is consistent across both channels.** This fork retains its owner/explicit-allowlist policy for all controls; upstream allowed anyone who could write in a thread to drive that session. This is a deliberate retained fork policy, not a backend discrepancy.
- **Optional services need their accounts.** Claude and Feldspar cannot execute without a Claude login. S3 needs a configured bucket and credentials. Their absence is not reported as a successful run.

## Verification and limits

- Full upstream command/parameter/permission coverage and exact source-file hash checks.
- Tests for channel isolation, a single gateway/command registry, existing-state/webhook migration, message-attached launches, uploads, permission decisions, stale-request rejection, mute/history, countdown persistence, lifecycle operations and backup recovery.
- Real Codex daemon checks: new turn and event normalization, rename, permission-mode updates, history, native fork retaining conversation context, restart, kill and resume.
- Real terminal check: attach a TUI, see the existing conversation reply, send a key, and verify the daemon has no duplicate conversation; terminal attachment does not change model/permission settings.
- Linux deployment checks cover services, command registration and channel configuration.
- **No live Claude inference test**: the operator has no Claude account and explicitly requested that authentication be left disabled. Original Claude logic is preserved and routing is tested, but this is not a claim of a live two-engine end-to-end test.
- S3 transport is exercised with a fake client; no production bucket or destructive offload was used for validation.

A shared command name alone is not evidence of parity. Keep this audit and the upstream pin current when updating upstream or changing backend mappings.
