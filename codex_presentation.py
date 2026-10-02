"""Normalize Codex records for the original upstream Chert renderers.

No second implementation of titles, identities, avatars, or activity-card markup.
"""
import time
from pathlib import Path

import discord_bot as upstream


def prompt_name(prompt):
    return upstream.slug(prompt)


def thread_title(name, ended=False):
    title = upstream.thread_title(name, '', False)
    return upstream.ended_title(title) if ended else title


def speaker_name(session):
    return upstream.webhook_name({'project': Path(session.cwd).name or 'codex', 'name': session.name})


def avatar_url(session):
    return upstream.avatar_url(str(session.discord_thread))


def activity_text(session, status, started=0, detail='', now=None):
    state = {'running': 'busy', 'idle': 'idle', 'error': 'error', 'interrupted': 'idle',
             'ended': 'ended', 'disconnected': 'ended'}.get(status, status)
    clock = time.time() if now is None else now
    # card_text reads its own clock; shift the start for deterministic callers.
    started = started or session.activity.get('started') or clock
    shifted = time.time() - (clock - started)
    card = dict(session.activity)
    card.update(started=shifted)
    if detail:
        card['desc'] = detail
    text = upstream.card_text({'status': state}, card, final=status == 'idle',
                              model=session.display_model or session.model or 'Codex default')
    if status == 'error':
        text = '❌ turn failed\n' + text
    if status == 'interrupted':
        text = '⏹ stopped\n' + text
    return text
