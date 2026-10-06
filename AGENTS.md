# Chert deployment

Deploy Chert to the existing **lemma** VM on **spoon**. The old VM named
`chert` was deleted; do not recreate it for deployments.

## Environment

- SSH host: `cheru@spoon`
- OrbStack executable on spoon: `/usr/local/bin/orb`
- Target VM: `lemma` (Arch Linux ARM)
- Linux user: `cheru`
- Production checkout: `/home/cheru/chert`
- Python environment: `/home/cheru/chert/.venv`
- Local Mac checkout: `/Users/cheru/Code/chert`
- GitHub fork: <https://github.com/recursiveforte/chert>
- Upstream source of truth for Chert functionality: <https://github.com/ceselder/chert>
- Production branch: `main`. Read the deployed commit with `git rev-parse HEAD`;
  do not assume the deployed revision matches the local checkout or GitHub.

Run commands inside lemma through spoon:

```bash
ssh cheru@spoon '/usr/local/bin/orb -m lemma -w /home/cheru/chert bash -s' <<'SH'
set -euo pipefail
# Linux commands here
SH
```

## Services and runtime

- `chert-discord-bridge`: Discord bot; runs `.venv/bin/python chert.py`.
- `chert-checkin`: dashboard; listens on `127.0.0.1:8899`.
- `chert-codex-daemon`: starts the shared Codex daemon.

Systemd units are in `/etc/systemd/system/`. `sudo` works noninteractively.
The bridge's local health endpoint is `http://127.0.0.1:8897/health`.

Chert connects to the existing Codex app-server through
`/home/cheru/.codex/app-server-control/app-server-control.sock`. Discord session
threads map to native Codex conversation IDs. Restarting the Discord bridge does
not require restarting Codex, and existing conversations must remain running.

## Preserve production state

- Keep the shared Codex daemon running during ordinary deployments.
- Preserve `/home/cheru/.codex`, its authentication, and native conversations.
- Preserve production `.env`, `bot_state.json`, and `private/` contents. Discord
  conversation mappings are in `private/codex-state.json`.
- Do not print or copy credentials into chat or Git. Keep backups containing
  credentials or conversations private.
- Claude is intentionally disabled with `CLAUDE_ENABLED=0`; leave it disabled
  unless the user requests otherwise.
- Preserve existing Discord channel IDs, permissions, and threads.
- Do not rerun `setup.sh` or `setup_discord.py` for an ordinary code update.
  The `setup.sh --dry-run` validation below is safe in a temporary checkout.
- Check for uncommitted work in both local and production checkouts. Preserve
  unrelated or concurrent work; do not assume it is already on GitHub, stage it
  indiscriminately, reset it, or overwrite it.

## Deployment procedure

1. Identify the intended changes and revision. Commit and push only the intended
   changes to `recursiveforte/chert` before deploying.
2. Check production `git status`. Inspect and preserve any local changes before
   updating the checkout.
3. Fetch the target revision and test it in a separate temporary checkout with
   its own virtual environment and `requirements.txt` dependencies. Run:

   ```bash
   python -m unittest discover -s tests -v
   bash -n setup.sh
   ./setup.sh --dry-run
   ```

   Run tests from that temporary checkout, not the production directory.
4. When native integration checks are relevant, use
   `tests/live_codex_activity.py`. Its `--model` option tests the parameterless
   model picker and actual model switching. These checks isolate `CODEX_HOME`
   and the app-server. Do not publish test threads into production Discord or
   run test prompts in the user's existing conversations.
5. Record the previous deployed commit. Back up `.env`, `bot_state.json`, and
   `private/` securely before any state migration.
6. Fast-forward production `main` to the intended revision. Update its virtual
   environment dependencies only if needed.
7. Restart `chert-discord-bridge`. Restart `chert-checkin` if dashboard code or
   dependencies changed. Do not restart `chert-codex-daemon` for these updates.
8. Verify all of the following:
   - Production `HEAD` matches the intended revision.
   - Services are active with no restart loop.
   - Startup logs show the Discord gateway connected.
   - `http://127.0.0.1:8897/health` returns HTTP 200.
   - `http://127.0.0.1:8899/codex/` returns HTTP 200.
   - Any changed slash-command schema is registered in Discord.
   - The existing Codex daemon remained running.
9. Report the deployed commit and actual verification results. If startup fails,
   restore the previous code/dependencies and restart the bridge. Restore state
   only if required by an incompatible migration, preserving newer conversation
   state where possible.

An active service or registered command alone does not prove that a feature
works. Validate the requested behavior and distinguish implemented features from
features actually exercised live; keep `docs/upstream-parity.md` accurate.
