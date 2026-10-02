#!/usr/bin/env bash
# Install the shared frontend (default), or select a single compatibility backend.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
APP="$(pwd)"
VENV="$APP/.venv"
BACKEND="${CHERT_BACKEND:-}"
SKIP_DISCORD=0
NO_SYSTEMD=0
DRY=0
while (( $# )); do
  case "$1" in
    --backend) BACKEND="${2:?--backend needs both, codex or claude}"; shift ;;
    --skip-discord) SKIP_DISCORD=1 ;;
    --no-systemd) NO_SYSTEMD=1 ;;
    --dry-run) DRY=1 ;;
    -h|--help)
      echo 'Usage: ./setup.sh [--backend both|codex|claude] [--skip-discord] [--no-systemd] [--dry-run]'
      exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done
PY="$(command -v python3.12 || command -v python3 || true)"
[[ -n "$PY" ]] || { echo 'Python 3.11+ is required'; exit 1; }
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ is required"'
if (( DRY )); then
  echo "Would install dependencies, configure ${BACKEND:-the saved backend (default codex)}, and provision Discord."
  echo 'Would install services unless --no-systemd is set; no files have been changed.'
  exit 0
fi
[[ -d "$VENV" ]] || "$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install -q -r requirements.txt
if [[ -z "$BACKEND" ]]; then
  BACKEND="$("$VENV/bin/python" -c 'from dotenv import dotenv_values; print(dotenv_values(".env").get("CHERT_BACKEND") or "both")')"
fi
[[ "$BACKEND" == both || "$BACKEND" == codex || "$BACKEND" == claude ]] || { echo 'Backend must be both, codex or claude'; exit 1; }
if [[ "$BACKEND" == codex || "$BACKEND" == both ]]; then
  CODEX="${CODEX_BIN:-$("$VENV/bin/python" -c 'from dotenv import dotenv_values; print(dotenv_values(".env").get("CODEX_BIN") or "")')}"
  [[ -n "$CODEX" ]] || CODEX="$(command -v codex || true)"
  [[ -n "$CODEX" ]] || CODEX="$HOME/.local/bin/codex"
  [[ -x "$CODEX" ]] || { echo 'Install the Codex CLI, run codex login, then rerun setup. See README.md.'; exit 1; }
fi
command -v tmux >/dev/null || { echo 'Install tmux, then rerun setup.'; exit 1; }
if [[ "$BACKEND" == claude || "$BACKEND" == both ]]; then
  if ! command -v claude >/dev/null && [[ ! -x "$HOME/.local/bin/claude" ]]; then
    echo 'Claude Code is not installed. #claude will need installation and login before use.'
  fi
fi
umask 077
"$VENV/bin/python" - "$BACKEND" "${CODEX:-}" <<'PY'
import json
import sys
import shutil
from pathlib import Path
from dotenv import dotenv_values
from setup_discord import ENV, read_env, write_env
backend = sys.argv[1]
previous = read_env()
previous_backend = previous.get('CHERT_BACKEND') or 'claude'
if previous.get('DISCORD_CHANNEL_ID') and previous_backend in {'claude', 'codex'}:
    key = 'DISCORD_CLAUDE_CHANNEL_ID' if previous_backend == 'claude' else 'DISCORD_CODEX_CHANNEL_ID'
    if not previous.get(key):
        write_env({key: previous['DISCORD_CHANNEL_ID']})
if not ENV.exists():
    example = Path('docs/claude.env.example' if backend == 'claude' else '.env.example')
    # Omit blank options so defaults work (int("") and Path("") don't).
    values = {k: v for k, v in dotenv_values(example).items() if v}
    write_env({k: json.dumps(v) if any(c.isspace() for c in v) else v for k, v in values.items()})
write_env({'CHERT_BACKEND': backend, 'PYTHONUNBUFFERED': '1'})
if backend in {'codex', 'both'}:
    binary = str(Path(sys.argv[2]).resolve())
    write_env({'CODEX_BIN': json.dumps(binary)})
    # npm launchers use /usr/bin/env node; include its stable installation path.
    node = shutil.which('node')
    paths = [str(Path.home() / '.local/bin'), str(Path(binary).parent)]
    if node:
        paths.append(str(Path(node).resolve().parent))
    paths += ['/usr/local/bin', '/usr/bin', '/bin']
    write_env({'PATH': json.dumps(':'.join(dict.fromkeys(paths)))})
if backend in {'codex', 'both'} and not read_env().get('PROJECT_ROOT'):
    projects = Path.home() / 'projects'
    projects.mkdir(exist_ok=True)
    write_env({'PROJECT_ROOT': str(projects)})
PY
chmod 600 .env
mkdir -p "$HOME/.local/bin"
for helper in bin/*; do install -m 755 "$helper" "$HOME/.local/bin/$(basename "$helper")"; done
"$VENV/bin/python" - <<'PY'
import os
import secrets
from pathlib import Path
from setup_discord import read_env
target = Path(read_env().get('HEARTH_HOOK_SECRET') or Path.home()/'.claude/hearth-hook.secret')
target.parent.mkdir(parents=True, exist_ok=True)
if not target.exists():
    with os.fdopen(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as file:
        file.write(secrets.token_hex(24) + '\n')
PY
if (( ! SKIP_DISCORD )); then
  "$VENV/bin/python" setup_discord.py --backend "$BACKEND"
fi
if [[ "$BACKEND" == claude || "$BACKEND" == both ]]; then
  mkdir -p "$HOME/.claude" "$HOME/.local/bin"
  "$VENV/bin/python" hooks/install_hooks.py
fi
mkdir -p systemd/rendered
"$VENV/bin/python" - "$BACKEND" <<'PY'
import getpass
import sys
from pathlib import Path
from setup_discord import read_env
root = Path.cwd()
names = ['chert-discord-bridge', 'chert-checkin', 'chert-tmux']
if sys.argv[1] in {'codex', 'both'}:
    names += ['chert-codex-daemon']
values = {'@USER@': getpass.getuser(), '@HOME@': str(Path.home()), '@APP@': str(root), '@VENV@': str(root / '.venv')}
config = read_env()
values.update({'@CODEX@': config.get('CODEX_BIN') or str(Path.home()/'.local/bin/codex'),
               '@CODEX_HOME@': config.get('CODEX_HOME') or str(Path.home()/'.codex'),
               '@RUNTIME_PATH@': config.get('PATH') or '/usr/local/bin:/usr/bin:/bin'})
for name in names:
    text = (root / 'systemd/templates' / (name + '.service.in')).read_text()
    for old, new in values.items():
        text = text.replace(old, new)
    if name == 'chert-discord-bridge':
        text = text.replace('After=network-online.target', 'After=network-online.target chert-tmux.service')
        text = text.replace('Wants=network-online.target', 'Wants=network-online.target chert-tmux.service')
    if name == 'chert-discord-bridge' and sys.argv[1] in {'codex', 'both'}:
        text = text.replace('After=network-online.target', 'After=network-online.target chert-codex-daemon.service')
        text = text.replace('Wants=network-online.target', 'Wants=network-online.target chert-codex-daemon.service')
    (root / 'systemd/rendered' / (name + '.service')).write_text(text)
PY
if (( NO_SYSTEMD )) || ! command -v systemctl >/dev/null; then
  echo "Configured $BACKEND. Start manually: .venv/bin/python chert.py"
  exit 0
fi
UNITS=(chert-discord-bridge chert-checkin)
RUNTIMES=(chert-tmux)
if [[ "$BACKEND" == codex || "$BACKEND" == both ]]; then RUNTIMES+=(chert-codex-daemon); fi
UNITS+=("${RUNTIMES[@]}")
for unit in "${UNITS[@]}"; do sudo install -m 644 "systemd/rendered/$unit.service" /etc/systemd/system/; done
sudo systemctl daemon-reload
if (( SKIP_DISCORD )); then
  echo 'Services installed but not started. Configure Discord, then run: sudo systemctl enable --now chert-discord-bridge'
else
  sudo systemctl enable "${UNITS[@]}"
  if (( ${#RUNTIMES[@]} )); then sudo systemctl start "${RUNTIMES[@]}"; fi
  sudo systemctl restart chert-discord-bridge chert-checkin
  echo "Chert ($BACKEND) started. Logs: journalctl -u chert-discord-bridge -f"
fi
