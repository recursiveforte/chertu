"""Codex session commands and interaction controls."""

import asyncio
import logging
from pathlib import Path
import time
import uuid
import re

import discord

from chert.vendor import bridge as upstream
from chert.backends.codex import storage as codex_storage
from chert.backends.codex.client import RpcError, live_status

LOG = logging.getLogger(__name__)


class CodexControls:
    def __init__(self, backend):
        self.backend = backend

    async def execute(self, name, channel, user, args, respond):
        if name in {"model", "globalmodel", "fast"} and user.id != self.backend.owner:
            return await respond(f"/{name} is owner-only.")
        if name == "help":
            from chert.discord.commands import help_text

            for part in upstream.split_chunks(help_text(self.backend.frontend.tree)):
                await respond(part)
            return
        if name == "resume":
            query = args.get("session", "")
            rows = await self.search_sessions(query)
            if len(rows) != 1:
                await channel.send(
                    upstream.resume_text(query, 0, rows),
                    view=upstream.resume_view(query, 0, rows),
                    allowed_mentions=upstream.NO_PING,
                )
                return await respond("Choose a session from the list.")
            thread = await self.backend.start_session("", codex_id=rows[0]["id"])
            return await respond(f"♻️ resumed → {thread.mention}")
        if name in {"disk", "backup", "s3", "offload", "restore"}:
            text = "!" + name
            if args.get("directory"):
                text += " " + args["directory"]
            if args.get("confirm"):
                text += " confirm"
            proxy = SimpleMessage(channel, user, text)
            await self.backend.frontend.handle_main_command(proxy, text)
            return await respond("Done.")
        if name == "globalmodel":
            model = await self.resolve_model(args["name"])
            self.backend.store.meta["model"] = model
            self.backend.config.model = model
            for s in self.backend.store.sessions.values():
                s.model = model
                if s.status != "ended":
                    s.pending_settings["model"] = model
            self.backend.store.save()
            from chert.provisioning import ENV, write_env

            if ENV.exists():
                write_env({"CODEX_MODEL": model})
            return await respond(
                f"🧠 Model for new sessions: `{model}` · existing sessions switch on their next turn"
            )
        if name == "fast":
            current = self.backend.store.sessions.get(channel.id)
            targets = (
                list(self.backend.store.sessions.values())
                if args.get("everywhere") or current is None
                else [current]
            )
            tier = "fast" if args.get("mode", "on") == "on" else "default"
            for target in targets:
                target.service_tier = tier
            if args.get("everywhere") or current is None:
                self.backend.store.meta["service_tier"] = tier
            self.backend.store.save()
            return await respond(
                "Fast setting saved for subsequent turns; account/model availability is enforced by Codex."
            )
        if name == "feldspar":
            if not self.backend.frontend.claude_enabled:
                return await respond(
                    "Feldspar uses both Claude and Codex reviewers. Sign in to Claude to enable it."
                )
            current = self.backend.store.sessions.get(channel.id)
            focus = args.get("focus", "")
            if current:
                transcript = (
                    self.backend.store.path.parent
                    / "codex-transcripts"
                    / f"{current.codex_thread}.jsonl"
                )
                target = {
                    "cwd": current.cwd,
                    "name": current.name,
                    "sid": current.codex_thread,
                    "transcript": str(transcript) if transcript.exists() else None,
                }
            else:
                cwd, words = upstream.resolve_project(focus.split())
                target = {"cwd": str(cwd), "name": cwd.name, "sid": None, "transcript": None}
                focus = " ".join(words)
            return await self.backend.frontend.feldspar(
                self.backend.frontend.main_channel, user, target, focus, respond
            )
        if name == "yolo":
            duration = args["duration"]
            seconds = 0 if duration == "off" else upstream.parse_duration(duration)
            if seconds is None or seconds > upstream.YOLO_MAX:
                raise ValueError("Use a duration such as 30m or 1h (at most 12h), or off.")
            self.backend.store.meta["yolo_until"] = time.time() + seconds if seconds else 0
            self.backend.store.save()
            return await respond(
                "Bypass for new Codex sessions enabled until the countdown expires."
                if seconds
                else "Bypass disabled for new sessions."
            )
        session = self.backend.store.sessions.get(channel.id)
        if session is None:
            raise ValueError("Use this command in a session thread.")
        if session.status == "ended" and name not in {
            "revive",
            "fork",
            "log",
            "mute",
            "unmute",
            "kill",
        }:
            return await respond("🌌 This session ended. Use /revive or /resume first.")
        if name in {"screen", "key"}:
            await self.backend.ensure_live(session)
            if name == "key":
                key = upstream.KEYMAP.get(args.get("key", "").lower())
                if not key:
                    raise ValueError("Choose a key from /key autocomplete or !help.")
                await self.backend.terminal.key(session, key)
                self.backend.store.save()
                return await respond("✅ Key sent.")
            text = await self.backend.terminal.screen(session)
            self.backend.store.save()
            options = upstream.parse_options(text)[0] if session.status == "waiting" else []
            await channel.send(
                upstream.prompt_body(text),
                view=upstream.prompt_view("codex:" + str(session.discord_thread), options),
                allowed_mentions=upstream.NO_PING,
            )
            return await respond("🖥 Terminal view updated.")
        if name in {"mute", "unmute"}:
            session.muted = name == "mute"
            self.backend.store.save()
            return await respond("🔇 muted" if session.muted else "🔊 unmuted")
        if name in {"kill", "stop"}:
            await self.backend.stop(channel, end=name == "kill")
            if name == "kill" and args.get("how") == "delete":
                await channel.delete()
            return await respond("Done.")
        if name in {"restart", "revive", "refresh"}:
            if name == "revive" and args.get("mode") == "fork":
                return await self.fork(session, "", respond)
            force = args.get("force") or args.get("mode") == "force"
            if name == "restart" and session.status == "running" and not force:
                return await respond("This session is busy; use force to interrupt it.")
            if name == "restart" or (name == "refresh" and force):
                await self.restart(session, channel, force)
            else:
                await self.revive(session, channel, force or name == "refresh")
            if args.get("message"):
                await self.backend.send_prompt(channel, args["message"])
            return await respond("♻️ session ready; history and Discord thread retained.")
        if name == "fork":
            if args.get("to") == "claude":
                return await self.handoff_to_claude(session, args.get("message", ""), user, respond)
            return await self.fork(session, args.get("message", ""), respond)
        if name == "log":
            await self.backend.live.connect()
            result = await self.backend.live.call(
                "thread/turns/list",
                {
                    "threadId": session.codex_thread,
                    "limit": min(int(args.get("count", 25)), 200),
                    "itemsView": "summary",
                    "sortDirection": "desc",
                },
            )
            entries = []
            for turn in reversed(result["data"]):
                for item in turn.get("items", []):
                    body = (
                        item.get("text")
                        or item.get("command")
                        or item.get("query")
                        or item.get("tool")
                        or item.get("type", "")
                    )
                    if item.get("type") == "userMessage":
                        body = " ".join(x.get("text", "") for x in item.get("content", []))
                    kind = {"agentMessage": "assistant", "userMessage": "user"}.get(
                        item.get("type"), "tool"
                    )
                    entries.append(("", kind, str(body)))
            text = upstream.render_ship_log(
                session.name, entries[-min(int(args.get("count", 25)), 200) :]
            )
            for chunk in upstream.split_chunks(text)[:4]:
                await respond(chunk)
            return
        if name == "supernova":
            old = session.deadline
            if args.get("cancel"):
                session.deadline = None
                if old and old.get("message"):
                    await channel.get_partial_message(old["message"]).edit(
                        content="☀️ Countdown cancelled."
                    )
            else:
                seconds = args.get("seconds") or int(args.get("minutes", 22)) * 60
                session.deadline = {
                    "until": time.time() + seconds,
                    "action": args.get("then", "wrap"),
                    "warned": [],
                }
                card = await channel.send(
                    f"☀️ Supernova <t:{int(session.deadline['until'])}:R>",
                    allowed_mentions=upstream.NO_PING,
                )
                session.deadline["message"] = card.id
            self.backend.store.save()
            return await respond(
                "Countdown cancelled."
                if not session.deadline
                else f"☀️ Supernova <t:{int(session.deadline['until'])}:R>"
            )
        if name in {"model", "effort", "rename"}:
            await self.backend.ensure_live(session)
            value = args.get("name") or args.get("level") or args.get("value", "")
            if name == "model":
                value = await self.resolve_model(value)
                session.pending_settings["model"] = value
            if name == "effort":
                from chert.config import EFFORTS

                if value not in (*EFFORTS, "default"):
                    raise ValueError("Choose a supported reasoning effort.")
                if value == "default":
                    cfg = await self.backend.live.call("config/read", {})
                    effort = cfg.get("config", {}).get("model_reasoning_effort")
                    if not effort:
                        raise ValueError(
                            "No default effort is configured; choose an explicit effort."
                        )
                    value = effort
                session.pending_settings.setdefault("config", {})["model_reasoning_effort"] = value
            if name == "rename":
                if not value.strip():
                    raise ValueError("Provide a session name.")
                await self.backend.live.call(
                    "thread/name/set", {"threadId": session.codex_thread, "name": value}
                )
                session.name = value.strip()[:90]
                await self.backend.events.observe_session(session, {"name": session.name})
            else:
                setattr(session, name, value)
            self.backend.store.save()
            return await respond(
                f"✅ {name} → `{value}`"
                + (" · applies to the next turn" if name in {"model", "effort"} else "")
            )
        if name == "mode":
            await self.set_mode(session, args["mode"])
            return await respond(f"Permission mode: {args['mode']}")
        raise ValueError(
            f"No Codex mapping for /{name}; this is a parity defect, not a successful command."
        )

    async def resolve_model(self, value):
        if value == "default":
            if self.backend.config.model:
                return self.backend.config.model
            await self.backend.live.connect()
            result = await self.backend.live.call("config/read", {})
            value = result.get("config", {}).get("model") or ""
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}", value):
            raise ValueError("Choose a model from autocomplete or provide an explicit model ID.")
        return value

    async def revive(self, session, channel, force):
        if force:
            await self.backend.stop(channel)
        await self.backend.ensure_live(session)
        info = (
            await self.backend.live.call(
                "thread/read", {"threadId": session.codex_thread, "includeTurns": False}
            )
        )["thread"]
        session.status = live_status(info)
        await channel.edit(archived=False)
        self.backend.store.save()

    async def restart(self, session, channel, force):
        if session.status == "running" and not force:
            raise ValueError("This session is busy; use force to interrupt it.")
        await self.backend.live.connect()
        if force:
            await self.backend.live.interrupt(session.codex_thread)
        # Archive/unarchive unloads only this thread's actor, not the shared daemon.
        # Always restore the persisted history before attempting to reload it.
        await self.backend.live.call("thread/archive", {"threadId": session.codex_thread})
        self.backend.live.subscribed.discard(session.codex_thread)
        await self.backend.live.call("thread/unarchive", {"threadId": session.codex_thread})
        await self.revive(session, channel, False)

    async def handoff_to_claude(self, session, message, user, respond):
        if not self.backend.frontend.claude_enabled:
            return await respond("Claude must be signed in before receiving a handoff.")
        await self.backend.ensure_live(session)
        history = await self.backend.live.call(
            "thread/turns/list",
            {
                "threadId": session.codex_thread,
                "limit": 100,
                "itemsView": "summary",
                "sortDirection": "desc",
            },
        )
        folder = Path(session.cwd)
        handoff = upstream.REPORTS_DIR / f"codex-handoff-{session.codex_thread}.md"
        handoff.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for turn in reversed(history["data"]):
            for item in turn.get("items", []):
                if item.get("type") == "userMessage":
                    lines.append(
                        "**USER:** " + "\n".join(p.get("text", "") for p in item.get("content", []))
                    )
                elif item.get("type") == "agentMessage":
                    lines.append("**CODEX:** " + item.get("text", ""))
                else:
                    lines.append(
                        f"[tool {item.get('type')}: {str(item.get('command') or item.get('tool') or '')[:220]}]"
                    )
        handoff.write_text(
            f"# Handoff from {session.name}\n\n"
            + "\n\n".join(lines)[-upstream.ASTRA_HANDOFF_CHARS :]
        )
        prompt = f"Continue the conversation saved in {handoff}. " + (
            message or "Read it and wait for my next instruction."
        )
        proxy = self.backend.frontend.main_channel
        pane, error = await asyncio.to_thread(
            upstream.spawn_claude, session.name + "-fork", str(folder)
        )
        if not pane:
            raise ValueError(error)
        running, notes = await self.backend.frontend.await_registration(pane)
        if not running:
            raise ValueError("Claude did not register; check its login and terminal.")
        thread = await self.backend.frontend.adopt_session(running, proxy)
        await asyncio.to_thread(self.backend.frontend.deliver, running, user, prompt)
        await respond(f"🌱 handoff → {thread.mention}")

    async def fork(self, session, message, respond):
        await self.backend.live.connect()
        result = await self.backend.live.call(
            "thread/fork", {"threadId": session.codex_thread, "excludeTurns": True}
        )
        await self.backend.live.call(
            "thread/name/set",
            {"threadId": result["thread"]["id"], "name": upstream.fork_name(session.name)},
        )
        thread = await self.backend.start_session("", codex_id=result["thread"]["id"])
        if message:
            await self.backend.send_prompt(thread, message)
        await respond(f"🌱 forked → {thread.mention}")

    async def search_sessions(self, query):
        await self.backend.live.connect()
        try:
            uuid.UUID(query)
        except ValueError:
            cache = getattr(self, "_index_cache", None)
            if cache is None or time.time() - cache[0] > 15:
                rows = []
                for archived in (False, True):
                    cursor = None
                    while True:
                        result = await self.backend.live.call(
                            "thread/list",
                            {
                                "limit": 100,
                                "archived": archived,
                                "cursor": cursor,
                                "sourceKinds": ["cli", "vscode", "exec", "appServer", "unknown"],
                            },
                        )
                        rows.extend(index_row(t) for t in result["data"])
                        cursor = result.get("nextCursor")
                        if not cursor:
                            break
                rows.sort(key=lambda r: -r["mtime"])
                existing = {r["sid"] for r in rows}
                backed_up = await asyncio.to_thread(codex_storage.backup_index)
                rows.extend(r for sid, r in backed_up.items() if sid not in existing)
                rows.sort(key=lambda r: -r["mtime"])
                self.backend._index_cache = (time.time(), rows)
            return upstream.search_sessions(query, self.backend._index_cache[1])
        try:
            result = await self.backend.live.call(
                "thread/read", {"threadId": query, "includeTurns": False}
            )
        except RpcError:
            rows = await asyncio.to_thread(codex_storage.backup_index)
            return [rows[query]] if query in rows else []
        return [index_row(result["thread"])]

    async def autocomplete(self, command, current):
        if command == "resume":
            rows = await self.search_sessions(current)
            return [
                discord.app_commands.Choice(
                    name=(r.get("name") or r.get("preview") or r["id"])[:100], value=r["id"]
                )
                for r in rows[:25]
            ]
        await self.backend.live.connect()
        rows = (await self.backend.live.call("model/list", {}))["data"]
        return [
            discord.app_commands.Choice(name=r["model"][:100], value=r["model"])
            for r in rows
            if current.lower() in r["model"].lower()
        ][:25]

    async def set_mode(self, session, mode):
        await self.backend.ensure_live(session)
        params = {"threadId": session.codex_thread, "excludeTurns": True}
        if mode == "bypass":
            params.update(approvalPolicy="never", sandbox="danger-full-access")
        elif mode in {"auto", "default"}:
            params.update(
                approvalPolicy="on-request",
                sandbox="workspace-write",
                approvalsReviewer="auto_review" if mode == "auto" else "user",
            )
        elif mode == "plan":
            session.collaboration_mode = "plan"
            session.permission_mode = mode
            self.backend.store.save()
            return
        else:
            raise ValueError("Choose default, auto, bypass, or plan.")
        await self.backend.live.call("thread/resume", params)
        session.collaboration_mode = "default"
        session.permission_mode = mode
        self.backend.store.save()

    async def on_component(self, interaction):
        cid = (interaction.data or {}).get("custom_id", "")
        if not self.backend.allowed(interaction.user.id, interaction.channel):
            return
        parts = cid.split("|")
        if len(parts) == 3 and parts[0] in {"o", "k"} and parts[1].startswith("codex:"):
            session = self.backend.store.sessions.get(interaction.channel_id)
            if not session or parts[1] != f"codex:{session.discord_thread}":
                return
            await interaction.response.defer()
            before = await self.backend.terminal.screen(session)
            kind, _, key = parts
            if key != "__refresh":
                await self.backend.terminal.key(session, key)
                if kind == "o":
                    await asyncio.sleep(1.2)
                    after = await self.backend.terminal.screen(session)
                    if (
                        upstream.parse_options(after)[0]
                        and upstream.parse_options(after)[0] == upstream.parse_options(before)[0]
                    ):
                        await self.backend.terminal.key(session, "Enter")
            text = await self.backend.terminal.screen(session)
            options = upstream.parse_options(text)[0] if session.status == "waiting" else []
            await interaction.message.edit(
                content=upstream.prompt_body(text),
                view=upstream.prompt_view(parts[1], options),
                allowed_mentions=upstream.NO_PING,
            )
            self.backend.store.save()
            return
        if cid.startswith("rp|"):
            _, query, page = cid.split("|")
            rows = await self.search_sessions(query)
            await interaction.response.edit_message(
                content=upstream.resume_text(query, int(page), rows),
                view=upstream.resume_view(query, int(page), rows),
            )
            return
        if cid.startswith("rs|"):
            sid = (interaction.data.get("values") or [None])[0]
            await interaction.response.defer(thinking=True)
            if sid:
                thread = await self.backend.start_session("", codex_id=sid)
                await interaction.followup.send(
                    f"♻️ resumed → {thread.mention}", allowed_mentions=upstream.NO_PING
                )
            return
        if cid.startswith("cx|"):
            key = cid.split("|")[1]
            if key not in self.backend.live.server_requests and not interaction.response.is_done():
                await interaction.response.send_message(
                    "That prompt expired. Use the newest prompt.", ephemeral=True
                )


class SimpleMessage:
    """Adapt slash controls to upstream’s text-command handler."""

    def __init__(self, channel, user, content):
        self.channel, self.author, self.content = channel, user, content
        self.attachments = []

    async def add_reaction(self, _emoji):
        pass


def index_row(info):
    path = Path(info["path"]) if info.get("path") else None
    return {
        **info,
        "sid": info["id"],
        "label": info.get("name") or info.get("preview", "")[:60] or info["id"][:8],
        "name": info.get("name") or "",
        "first": info.get("preview") or "",
        "project": Path(info.get("cwd") or ".").name,
        "cwd": info.get("cwd") or "",
        "mtime": info.get("updatedAt") or info.get("createdAt") or 0,
        "size": path.stat().st_size if path and path.exists() else 0,
        "where": "local",
        "live": (info.get("status") or {}).get("type") in {"active", "idle"},
    }


def text_arguments(command, value):
    if command in {"status", "threads", "ls"}:
        return {"_command": "sessions"}
    if command in {"bypass", "auto"}:
        return {"_command": "mode", "mode": command}
    if command == "unstick":
        return {
            "_command": "refresh",
            "force": value.startswith("force"),
            "message": value.removeprefix("force").strip(),
        }
    if command == "resume":
        return {"session": value}
    if command in {"model", "globalmodel", "rename"}:
        return {"name": value}
    if command == "effort":
        return {"level": value}
    if command == "key":
        return {"key": value}
    if command == "fork":
        words = value.split(maxsplit=1)
        if words and words[0] in {"codex", "astra", "claude"}:
            return {"to": words[0], "message": words[1] if len(words) > 1 else ""}
        return {"to": "same", "message": value}
    if command == "log":
        return {"count": int(value) if value.isdigit() else (200 if value == "all" else 25)}
    if command in {"kill", "revive"}:
        return {"how" if command == "kill" else "mode": value}
    if command in {"restart", "refresh"}:
        return {
            "force": value.startswith("force"),
            "message": value.removeprefix("force").strip() if command == "refresh" else "",
        }
    if command == "fast":
        return {
            "mode": value.split()[0] if value else "on",
            "everywhere": "everywhere" in value or "all" in value,
        }
    if command == "mode":
        return {"mode": value}
    if command == "yolo":
        return {"duration": value}
    if command == "feldspar":
        return {"focus": value}
    if command in {"offload", "restore"}:
        return {
            "directory": value.removesuffix(" confirm").strip(),
            "confirm": value.endswith(" confirm"),
        }
    if command == "supernova":
        words = value.split()
        seconds = (
            upstream.parse_duration(words[0])
            if words and words[0] not in {"off", "cancel"}
            else 22 * 60
        )
        if seconds is None:
            raise ValueError("Use a duration such as 22m, 1h, or off.")
        return {
            "cancel": value in {"off", "cancel"},
            "seconds": seconds,
            "then": words[1] if len(words) > 1 else "wrap",
        }
    return {}
