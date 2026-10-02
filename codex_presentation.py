"""The original Chert Discord presentation, shared by both Codex transports."""
from pathlib import Path
import re
import time
from urllib.parse import quote


def prompt_name(prompt):
    return re.sub(r'[^a-z0-9]+', '-', prompt.lower()).strip('-')[:32].rstrip('-') or 'adhoc'


def thread_title(name, ended=False):
    return f'{"🌌" if ended else "🚀"} {name}'[:100]


def speaker_name(session):
    project = Path(session.cwd).name or 'codex'
    name = session.name
    if name == project or name.startswith(project + '-'):
        return name[:76]
    room = min(24, 76 - len(project) - 3)
    if room < 8:
        return name[:76]
    short = name if len(name) <= room else name[:room].rstrip('-') + '…'
    return f'{project} · {short}'[:76]


def avatar_url(session):
    # Same generated robot style as upstream; a stable seed survives renames/resumes.
    return ('https://api.dicebear.com/9.x/bottts-neutral/png?size=128'
            '&backgroundColor=b6e3f4,c0aede,d1d4f9,ffd5dc,ffdfbf&seed='
            + quote(str(session.discord_thread)))


def activity_text(session, status, started=0, detail='', now=None):
    label = {'running': '🟡 working', 'idle': '✅ turn done', 'error': '❌ turn failed',
             'interrupted': '⏹ stopped', 'ended': '🌌 ended',
             'disconnected': '⚪ disconnected'}.get(status, status)
    parts = [label]
    if started:
        elapsed = max(0, int((time.time() if now is None else now) - started))
        parts.append(f'{elapsed // 60}m {elapsed % 60:02d}s')
    parts.append(f'🧠 `{session.display_model or session.model or "Codex default"}`')
    if detail:
        parts.append(' '.join(detail.split())[:200])
    return ' · '.join(parts)
