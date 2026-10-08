"""Codex status layout, with upstream identities and activity detail rendering."""

import time
from pathlib import Path
from urllib.parse import quote

import discord

from chert.vendor import bridge as upstream


def prompt_name(prompt):
    return upstream.slug(prompt)


def thread_title(name, ended=False, *, sid="", collides=False):
    title = upstream.thread_title(name, sid, collides)
    return upstream.ended_title(title) if ended else title


def speaker_name(session):
    return upstream.webhook_name(
        {"project": Path(session.cwd).name or "codex", "name": session.name}
    )


def activity_text(session, status, started=0, detail="", now=None, workspace=None):
    card = dict(session.activity)
    clock = time.time() if now is None else now
    if status not in {"running", "waiting"}:
        clock = card.get("completed_at") or (
            card.get("last_edit") if card.get("rendered_status") == status else None
        ) or clock
    has_turn = bool(started or card.get("started"))
    started = started or card.get("started") or clock
    # Preserve upstream elapsed-time, tool-count, and progress formatting.
    card["started"] = time.time() - max(0, clock - started)
    if detail:
        card["desc"] = detail
    final = status == "idle" and has_turn
    progress = upstream.card_text({"status": status}, card, final=final)
    progress = progress if final else "\n".join(progress.splitlines()[1:])
    if status == "disconnected":
        progress = "-# live updates unavailable"
    emoji, label = {
        "running": ("🔭", "working"),
        "waiting": ("🔔", "needs you"),
        "idle": ("✅", "done") if final else ("💤", "ready"),
        "error": ("❌", "turn failed"),
        "interrupted": ("⏹", "stopped"),
        "ended": ("🌌", "ended"),
        "disconnected": ("⚪", "disconnected"),
    }.get(status, ("⚪", status))
    name = discord.utils.escape_markdown(session.name.replace("\n", " ")[:150])
    workspace = (workspace or Path(session.cwd).name or "codex").replace("`", "'").replace("\n", " ")[:250]
    model = (session.display_model or session.model or "Codex default").replace("`", "'").replace("\n", " ")[:150]
    elapsed = ""
    if has_turn and not final:
        minutes, seconds = divmod(int(max(0, clock - started)), 60)
        elapsed = f" · {minutes}m {seconds:02d}s"
    head = (f"**{name}** · `{workspace}`\n"
            f"{emoji} **{label}**{elapsed} · updated <t:{int(clock)}:R>\n"
            f"-# 🧠 model `{model}`")
    dashboard = upstream.DASHBOARD.removesuffix("/claudes").removesuffix("/codex") + "/codex"
    if session.codex_thread:
        dashboard += "/s/" + quote(session.codex_thread, safe="")
    footer = f"-# [dashboard]({dashboard}) · reply to talk · `!screen` · `!help`"
    # Keep the controls intact even when native progress fills the message budget.
    budget = max(0, 1990 - len(head) - len(footer) - 2)
    if len(progress) > budget:
        progress = progress[:max(0, budget - 1)] + "…"
    return "\n".join(part for part in (head, progress, footer) if part)
