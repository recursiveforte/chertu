"""Codex notifications, transcript journaling, and activity rendering."""

import asyncio
import json
import logging
from pathlib import Path
import time
import uuid
from datetime import datetime, timezone
import os

import discord

from chert.vendor import bridge as upstream
from chert.backends.codex.client import live_status

LOG = logging.getLogger(__name__)
TOOL_NAMES = {
    "commandExecution": "Bash",
    "fileChange": "Edit",
    "mcpToolCall": "MCP",
    "webSearch": "WebSearch",
    "collabAgentToolCall": "Agent",
}


class CodexEvents:
    def __init__(self, backend):
        self.backend = backend
        self.event_locks = {}
        self.card_locks = {}

    def record(self, session, kind, text, key=None, tool_id=None, error=False):
        if key and key in session.journal_seen:
            return
        if key:
            session.journal_seen = (session.journal_seen + [key])[-1000:]
        session.recent = (
            session.recent + [{"kind": kind, "text": str(text)[:8000], "time": time.time()}]
        )[-200:]
        folder = self.backend.store.path.parent / "codex-transcripts"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        timestamp = datetime.now(timezone.utc).isoformat()
        if kind == "user":
            row = {
                "type": "user",
                "origin": {"kind": "human"},
                "timestamp": timestamp,
                "message": {"content": str(text)},
            }
        elif kind in {"agentMessage", "thinking"}:
            row = {
                "type": "assistant",
                "timestamp": timestamp,
                "message": {
                    "content": (
                        [{"type": "text", "text": str(text)}]
                        if kind == "agentMessage"
                        else [{"type": "thinking", "thinking": str(text)}]
                    )
                },
            }
        elif kind == "result":
            row = {
                "type": "user",
                "timestamp": timestamp,
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": str(text),
                            "is_error": error,
                        }
                    ]
                },
            }
        else:
            tool_name = TOOL_NAMES.get(kind, kind)
            row = {
                "type": "assistant",
                "timestamp": timestamp,
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": key or "",
                            "name": tool_name,
                            "input": {"command": str(text)},
                        }
                    ]
                },
            }
        path = folder / f"{session.codex_thread}.jsonl"
        with os.fdopen(os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600), "a") as file:
            file.write(json.dumps(row) + "\n")
        self.backend.store.save()

    async def handle_live_event(self, event):
        if event["method"] == "chert/disconnected":
            for session in list(self.backend.store.sessions.values()):
                if session.backend != "app-server" or session.status in {"ended", "disconnected"}:
                    continue
                async with self.event_locks.setdefault(session.codex_thread, asyncio.Lock()):
                    if session.status == "ended":
                        continue
                    session.status, session.delivery_failed = "disconnected", True
                    self.backend.store.save()
                    try:
                        await self.update_live_status(session)
                    except Exception:
                        LOG.exception(
                            "Could not display Codex disconnection for %s", session.codex_thread
                        )
            return
        sid = (event.get("params") or {}).get("threadId", "")
        async with self.event_locks.setdefault(sid, asyncio.Lock()):
            try:
                await self._handle_live_event(event)
            except Exception:
                session = next(
                    (s for s in self.backend.store.sessions.values() if s.codex_thread == sid), None
                )
                if session:
                    session.delivery_failed = True
                    self.backend.store.save()
                raise

    async def _handle_live_event(self, event):
        params = event.get("params") or {}
        session = next(
            (
                s
                for s in self.backend.store.sessions.values()
                if s.codex_thread == params.get("threadId")
            ),
            None,
        )
        if session and session.status == "ended":
            return
        if session:
            if event["method"] == "thread/name/updated":
                name = params.get("threadName") or params.get("name")
                if name:
                    await self.observe_session(session, {"name": name})
                return
            if event["method"] == "chert/inputRequired":
                return await self.show_request(session, event)
            if event["method"] == "turn/started":
                turn = params["turn"]
                if f"completed:{turn['id']}" in session.seen_live_items:
                    return
                self.begin_activity(session, turn["id"], turn.get("startedAt"))
                await self.update_live_status(session)
                return
            if event["method"] == "turn/completed":
                turn = params["turn"]
                if session.active_turn and session.active_turn != turn["id"]:
                    return
                if (
                    f"completed:{turn['id']}" in session.seen_live_items
                    and session.activity.get("completed_at")
                    and session.status in {"idle", "error", "interrupted"}
                ):
                    return
                session.last_completed_at = params["turn"].get("completedAt") or time.time()
                session.activity.update(completed_at=session.last_completed_at, dirty=True)
                if session.subagents:
                    session.subagents["closed"] = True
                self.backend.store.save()
            if event["method"] == "error" and params.get("willRetry"):
                detail = upstream._clean(
                    (params.get("error") or {}).get("message", "API request failed"), 200
                )
                session.activity.update(desc=f"⏳ Retrying: {detail}", dirty=True)
                if session.activity.get("retry_notice") != detail and not session.muted:
                    await self.backend.say(
                        await self.backend.live_channel(session),
                        f"-# ⏳ {detail} — Codex is retrying",
                    )
                    session.activity["retry_notice"] = detail
                await self.update_live_status(session)
                return
            if event["method"] in {
                "item/started",
                "item/completed",
                "item/agentMessage/delta",
                "item/reasoning/summaryTextDelta",
                "turn/plan/updated",
            }:
                turn_id = params.get("turnId")
                if (
                    turn_id
                    and session.activity.get("turn_id") != turn_id
                    and event["method"] != "item/completed"
                ):
                    self.begin_activity(session, turn_id)
            if event["method"] in {"item/agentMessage/delta", "item/reasoning/summaryTextDelta"}:
                kind = (
                    "thinking"
                    if event["method"] == "item/reasoning/summaryTextDelta"
                    else "assistant"
                )
                key = f"{params.get('itemId')}:{params.get('summaryIndex', 0)}:{kind}"
                if session.activity.get("delta_key") != key:
                    session.activity.update(delta_key=key, delta_text="")
                text = (session.activity.get("delta_text", "") + params.get("delta", ""))[-2000:]
                session.activity["delta_text"] = text
                upstream.update_card({"card": session.activity}, [{"kind": kind, "text": text}])
                await self.update_live_status(session)
                return
            if event["method"] == "turn/plan/updated":
                steps = params.get("plan") or []
                current = next(
                    (s.get("step", "") for s in steps if s.get("status") == "inProgress"), ""
                )
                session.activity.update(
                    desc=upstream._clean(current or params.get("explanation", ""), 200), dirty=True
                )
                await self.update_live_status(session)
                return
            if event["method"] in {"item/started", "item/completed"}:
                item = params.get("item") or {}
                kind = item.get("type")
                if event["method"] == "item/started" and session.activity.pop("retry_notice", None):
                    session.activity.update(desc="", dirty=True)
                if kind == "contextCompaction" and not session.muted:
                    key = f"{item.get('id')}:{event['method']}"
                    notices = session.activity.setdefault("compaction_notices", [])
                    if key not in notices:
                        text = (
                            "-# 🌀 compacting context…"
                            if event["method"] == "item/started"
                            else "-# 🌀 compacted — context is smaller now, memory intact"
                        )
                        await self.backend.say(await self.backend.live_channel(session), text)
                        notices.append(key)
                        self.backend.store.save()
                if kind == "collabAgentToolCall":
                    self.update_subagents(session, item.get("agentsStates") or {})
                if kind in {
                    "commandExecution",
                    "fileChange",
                    "mcpToolCall",
                    "webSearch",
                    "collabAgentToolCall",
                }:
                    session.activity.setdefault("counts", {})
                    session.activity.setdefault("started", time.time())
                    seen = session.activity.setdefault("seen_tools", [])
                    if item.get("id") not in seen:
                        seen.append(item.get("id"))
                        state = {"card": session.activity}
                        upstream.update_card(
                            state,
                            [
                                {
                                    "kind": "tool",
                                    "name": TOOL_NAMES[kind],
                                    "text": item.get("command")
                                    or item.get("query")
                                    or item.get("tool", ""),
                                    "desc": item.get("description", ""),
                                }
                            ],
                        )
                        session.activity = state["card"]
                        self.backend.store.save()
                        if not session.muted:
                            await self.update_live_status(session)
            if event["method"] == "item/completed":
                item = params.get("item") or {}
                key = f"{params.get('turnId', '')}:{item.get('id', '')}"
                if item.get("type") == "reasoning" and item.get("summary"):
                    summary = item["summary"]
                    text = (
                        summary
                        if isinstance(summary, str)
                        else "\n".join(
                            x.get("text", "") if isinstance(x, dict) else str(x) for x in summary
                        )
                    )
                    session.activity.setdefault("counts", {})
                    state = {"card": session.activity}
                    upstream.update_card(state, [{"kind": "thinking", "text": text}])
                    self.record(session, "thinking", text, key)
                    if not session.muted:
                        await self.update_live_status(session)
                if item.get("type") == "userMessage":
                    self.record(
                        session,
                        "user",
                        "\n".join(i.get("text", "") for i in item.get("content", [])),
                        key,
                    )
                # imageView means the agent inspected a local image, not that it
                # requested an upload. File delivery is explicit via hearth-send.
                if item.get("type") in {
                    "agentMessage",
                    "commandExecution",
                    "fileChange",
                    "mcpToolCall",
                    "webSearch",
                    "collabAgentToolCall",
                }:
                    self.record(
                        session,
                        item["type"],
                        item.get("text")
                        or item.get("command")
                        or item.get("query")
                        or item.get("tool")
                        or item.get("type"),
                        key,
                    )
                if item.get("type") == "commandExecution" and item.get("aggregatedOutput"):
                    self.record(
                        session,
                        "result",
                        item["aggregatedOutput"],
                        key + ":result",
                        tool_id=key,
                        error=item.get("exitCode") not in (None, 0),
                    )
                if item.get("type") == "agentMessage":
                    upstream.update_card(
                        {"card": session.activity},
                        [{"kind": "assistant", "text": item.get("text", "")}],
                    )
                if session.muted:
                    return
            if (
                event["method"] == "thread/status/changed"
                and params["status"].get("type") == "idle"
                and session.active_turn
            ):
                # Native idle can precede turn/completed. Only that event (or the
                # history reconciliation) can collapse a working card to "done".
                return
        if session is None:
            return
        if event["method"] == "item/completed":
            item = params.get("item") or {}
            key = f"{params.get('turnId', '')}:{item.get('id', '')}"
            if (
                item.get("type") != "agentMessage"
                or not item.get("text")
                or key in session.seen_live_items
            ):
                return
            await self.backend.say(await self.backend.live_channel(session), item["text"])
            session.seen_live_items = (session.seen_live_items + [key])[-256:]
            self.backend.store.save()
        elif event["method"] == "turn/completed":
            turn = params["turn"]
            key = f"completed:{turn['id']}"
            if key not in session.seen_live_items:
                session.seen_live_items = (session.seen_live_items + [key])[-256:]
                if turn["status"] == "completed":
                    session.turns += 1
            session.active_turn = None
            session.status = {"failed": "error", "interrupted": "interrupted"}.get(
                turn["status"], "idle"
            )
            if turn.get("error"):
                await self.backend.say(
                    await self.backend.live_channel(session),
                    f"Codex turn failed: {turn['error'].get('message', 'unknown error')}",
                )
            self.backend.store.save()
            await self.update_live_status(session)
        elif event["method"] == "thread/status/changed":
            session.status = live_status({"status": params["status"]})
            self.backend.store.save()
            await self.update_live_status(session)
        if (
            session
            and event["method"] == "turn/completed"
            and not session.delivery_failed
            and not event.get("reconciled")
        ):
            turn = params["turn"]
            session.mirrored_turns = (session.mirrored_turns + [turn["id"]])[-1000:]
            session.mirror_since = max(session.mirror_since, turn.get("completedAt") or time.time())
            self.backend.store.save()

    def begin_activity(self, session, turn_id, started=None):
        if session.activity.get("turn_id") != turn_id:
            session.turn_started = started or time.time()
            session.activity = {
                "turn_id": turn_id,
                "started": session.turn_started,
                "counts": {},
                "dirty": True,
            }
            session.status_message = None
        session.active_turn, session.status = turn_id, "running"
        self.backend.store.save()

    async def update_live_status(self, session):
        if session.muted:
            return
        from chert.backends.codex.presentation import activity_text

        async with self.card_locks.setdefault(session.discord_thread, asyncio.Lock()):
            if session.status == "ended":
                return
            now = time.time()
            card = session.activity
            if (
                session.status_message
                and card.get("rendered_status") == session.status
                and session.status in {"running", "waiting"}
                and now - card.get("last_edit", 0) < upstream.CARD_MIN_GAP
            ):
                return
            content = activity_text(session, session.status, session.turn_started)
            if card.get("body") == content and session.status_message:
                if card.get("dirty"):
                    card["dirty"] = False
                    self.backend.store.save()
                return
            channel = await self.backend.live_channel(session)
            if session.status_message:
                try:
                    if session.status_webhook:
                        webhook = await self.backend.webhook_for(
                            await self.backend.parent_channel(channel)
                        )
                        await webhook.edit_message(
                            session.status_message,
                            content=content,
                            thread=channel,
                            allowed_mentions=upstream.NO_PING,
                        )
                    else:
                        await channel.get_partial_message(session.status_message).edit(
                            content=content, allowed_mentions=upstream.NO_PING
                        )
                except discord.NotFound:
                    session.status_message = None
            if not session.status_message:
                message = await self.backend.say(channel, content)
                if message is None:
                    raise RuntimeError("Discord did not return an activity message")
                session.status_message, session.status_webhook = message.id, True
            card.update(last_edit=now, dirty=False, body=content, rendered_status=session.status)
            self.backend.store.save()

    async def activity_tick(self):
        # A slow board, catch-up, or another session's rate limit must not stop
        # the heartbeat. Failed edits remain dirty and retry on the next tick.
        async def refresh(session):
            pending = bool(session.activity) and (
                session.activity.get("dirty")
                or session.activity.get("rendered_status") != session.status
            )
            subs_pending = session.subagents.get("dirty")
            if (
                session.muted
                or session.status == "ended"
                or session.status not in {"running", "waiting"}
                and not pending
                and not subs_pending
            ):
                return
            gap = upstream.CARD_MIN_GAP if pending else upstream.CARD_HEARTBEAT
            if (
                subs_pending
                or not session.status_message
                or time.time() - session.activity.get("last_edit", 0) >= gap
            ):
                try:
                    async with asyncio.timeout(15):
                        async with self.event_locks.setdefault(
                            session.codex_thread, asyncio.Lock()
                        ):
                            if session.status == "ended":
                                return
                            await self.update_live_status(session)
                            if subs_pending:
                                state = {
                                    "thread": session.discord_thread,
                                    "parent": (
                                        await self.backend.parent_channel(
                                            await self.backend.live_channel(session)
                                        )
                                    ).id,
                                    "subs": session.subagents,
                                }
                                info = {
                                    "key": str(session.discord_thread),
                                    "project": Path(session.cwd).name,
                                    "name": session.name,
                                }
                                await self.backend.frontend.render_subs(
                                    session.codex_thread, info, state
                                )
                                session.subagents["dirty"] = False
                                self.backend.store.save()
                except Exception:
                    LOG.exception("Codex activity update failed for %s", session.codex_thread)

        await asyncio.gather(*(refresh(s) for s in list(self.backend.store.sessions.values())))

    def update_subagents(self, session, agents):
        state = {"subs": session.subagents}
        for agent_id, info in agents.items():
            status = info.get("status")
            previous = state["subs"].get("states", {}).get(agent_id)
            if status == previous:
                continue
            live_states = {"pendingInit", "running"}
            if previous and previous not in live_states and status not in live_states:
                state["subs"]["states"][agent_id] = status
                continue
            event = "SubagentStart" if status in live_states else "SubagentStop"
            # Reuse upstream's aggregation and renderer, including one edited
            # subagent line instead of publishing internal agents as threads.
            self.backend.frontend.hook_subagent(
                event, {"agent_id": agent_id, "agent_type": "Codex"}, state
            )
            state["subs"].setdefault("states", {})[agent_id] = status
            state["subs"]["dirty"] = True
        session.subagents = state["subs"]
        self.backend.store.save()

    async def activity_loop(self):
        await self.backend.wait_until_ready()
        while not self.backend.is_closed():
            await self.activity_tick()
            await asyncio.sleep(upstream.CARD_MIN_GAP)

    async def observe_session(self, session, info):
        if session.status == "ended":
            return
        if info.get("cwd"):
            session.cwd = info["cwd"]
        changed_name = bool(info.get("name") and info["name"] != session.name)
        if info.get("name"):
            session.name = info["name"]
        await self.update_thread_title(session)
        if changed_name:
            channel = await self.backend.live_channel(session)
            await self.backend.frontend.say(channel, f"-# ✏️ renamed to **{session.name}**")
        self.backend.store.save()

    async def update_thread_title(self, session):
        from chert.backends.codex.presentation import thread_title

        if session.status == "ended":
            return
        collision = any(
            s is not session and s.status != "ended" and s.name == session.name
            for s in self.backend.store.sessions.values()
        )
        # Codex IDs are UUIDv7, so their leading digits are a shared timestamp.
        # Use the entropy-bearing tail when upstream's renderer needs a short suffix.
        short_id = (session.codex_thread or "")[-4:]
        title = thread_title(session.name, sid=short_id, collides=collision)
        channel = await self.backend.live_channel(session)
        frontend = self.backend.frontend
        pending = session.discord_thread in frontend._retitling
        # Replace queued work even when the visible name already matches: an
        # older edit may still be sleeping on a Discord rate limit. Retry failed
        # edits on discovery even if the cached desired title hasn't changed.
        if title != session.thread_title_cache or (
            not pending and getattr(channel, "name", None) != title
        ):
            if pending or getattr(channel, "name", None) != title:
                frontend.retitle(channel, title)
            session.thread_title_cache = title
        self.backend.store.save()

    async def observe_status(self, session, info):
        """Repair missed lifecycle events, including attaching halfway through a turn."""
        from chert.backends.codex.client import live_status

        async with self.event_locks.setdefault(session.codex_thread, asyncio.Lock()):
            if session.status == "ended":
                return
            previous_status = session.status
            page = await self.backend.live.call(
                "thread/turns/list",
                {
                    "threadId": session.codex_thread,
                    "limit": 1,
                    "sortDirection": "desc",
                    "itemsView": "notLoaded",
                },
            )
            turn = next(iter(page.get("data", [])), None)
            if turn and turn["status"] == "inProgress":
                self.begin_activity(session, turn["id"], turn.get("startedAt"))
                if live_status(info) == "waiting":
                    session.status = "waiting"
            elif turn and (
                session.active_turn == turn["id"] or session.activity.get("turn_id") == turn["id"]
            ):
                was_active = bool(session.active_turn)
                await self._handle_live_event(
                    {
                        "method": "turn/completed",
                        "reconciled": True,
                        "params": {"threadId": session.codex_thread, "turn": turn},
                    }
                )
                if was_active:
                    session.delivery_failed = (
                        True  # Recover any replies whose notifications were missed too.
                    )
                    self.backend.store.save()
                return
            else:
                session.status = live_status(info)
            self.backend.store.save()
            if (
                previous_status != session.status
                or not session.status_message
                or not session.activity.get("body")
            ):
                await self.update_live_status(session)

    async def catch_up(self, session):
        """Recover persisted replies missed during a bridge disconnect; never rerun a turn."""
        async with self.event_locks.setdefault(session.codex_thread, asyncio.Lock()):
            if session.status == "ended":
                return
            if not session.mirror_since:
                session.mirror_since = discord.utils.snowflake_time(
                    session.discord_thread
                ).timestamp()
            known = set(session.mirrored_turns)
            if not session.delivery_failed:
                known.update(
                    k.removeprefix("completed:")
                    for k in session.seen_live_items
                    if k.startswith("completed:")
                )
            cursor, pending = None, []
            try:
                while True:
                    page = await self.backend.live.call(
                        "thread/turns/list",
                        {
                            "threadId": session.codex_thread,
                            "limit": 10,
                            "sortDirection": "desc",
                            "itemsView": "summary",
                            "cursor": cursor,
                        },
                    )
                    reached_checkpoint = False
                    for turn in page["data"]:
                        if turn["status"] == "inProgress":
                            session.active_turn = turn["id"]
                            continue
                        timestamp = turn.get("completedAt") or turn.get("startedAt") or 0
                        if not timestamp:
                            try:
                                identifier = uuid.UUID(turn["id"])
                                if identifier.version == 7:
                                    timestamp = int(identifier.hex[:12], 16) / 1000
                            except ValueError:
                                pass
                        if turn["id"] != session.active_turn and (
                            not timestamp or timestamp + 1 < session.mirror_since
                        ):
                            reached_checkpoint = True
                            continue
                        if turn["id"] not in known:
                            pending.append(turn)
                    cursor = page.get("nextCursor")
                    if reached_checkpoint or not cursor:
                        break
                for turn in reversed(pending):
                    for item in turn.get("items", []):
                        if item.get("type") == "agentMessage":
                            await self._handle_live_event(
                                {
                                    "method": "item/completed",
                                    "params": {
                                        "threadId": session.codex_thread,
                                        "turnId": turn["id"],
                                        "item": item,
                                    },
                                }
                            )
                    if turn.get("error"):
                        await self.backend.say(
                            await self.backend.live_channel(session),
                            f"⚠️ Recovered failed turn: {turn['error'].get('message', 'unknown error')}",
                        )
                    if (
                        turn["status"] == "completed"
                        and f"completed:{turn['id']}" not in session.seen_live_items
                    ):
                        session.turns += 1
                    session.mirrored_turns = (session.mirrored_turns + [turn["id"]])[-1000:]
                    session.seen_live_items = (
                        session.seen_live_items + [f"completed:{turn['id']}"]
                    )[-256:]
                    session.mirror_since = max(
                        session.mirror_since, turn.get("completedAt") or session.mirror_since
                    )
                    self.backend.store.save()
                session.delivery_failed = False
                self.backend.store.save()
            except Exception:
                session.delivery_failed = True
                self.backend.store.save()
                raise

    async def show_request(self, session, event):
        from chert.discord.prompts import request_view

        key = event.get("requestKey")
        request = self.backend.live.server_requests.get(key)
        if request:
            session.status = "waiting"
            self.backend.store.save()
            await self.update_live_status(session)
            body, view = request_view(self.backend, key, request)
            ping = f"<@{self.backend.owner}> " if self.backend.owner else ""
            await (await self.backend.live_channel(session)).send(
                ping + body,
                view=view,
                allowed_mentions=discord.AllowedMentions(
                    users=[discord.Object(id=self.backend.owner)] if self.backend.owner else [],
                    roles=False,
                    everyone=False,
                ),
            )

    async def run(self):
        await self.backend.wait_until_ready()
        while not self.backend.is_closed():
            event = await self.backend.live.notifications.get()
            try:
                await self.handle_live_event(event)
            except Exception:
                LOG.exception("Could not mirror Codex event to Discord")
            finally:
                self.backend.live.notifications.task_done()
