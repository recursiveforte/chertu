"""Map native Codex queue/turn identities to Discord prompt reactions.

Codex owns all queued content and dispatch. Only message IDs and reaction state
are persisted here; reconciliation never submits or replays a prompt.
"""

import asyncio
import logging
import time

import discord

from chert.backends.codex.client import RpcError

LOG = logging.getLogger(__name__)
EMOJI = ("↪️", "👀", "✅", "❌")


class PromptReactions:
    def __init__(self, backend):
        self.backend = backend
        self.messages = {}

    async def click(self, payload):
        frontend = self.backend.frontend
        if (
            str(payload.emoji).replace("\ufe0f", "") != "↪"
            or getattr(payload.emoji, "id", None) is not None
            or payload.guild_id != frontend.projects.guild_id
            or payload.user_id == frontend.user.id
            or getattr(payload.member, "bot", False)
            or not frontend.allowed_user(discord.Object(id=payload.user_id))
        ):
            return
        session = next(
            (
                s
                for s in self.backend.store.sessions.values()
                if any(
                    e["message"] == payload.message_id and e["channel"] == payload.channel_id
                    for e in s.prompt_messages
                )
            ),
            None,
        )
        if session is None:
            return
        channel = self.backend.get_channel(payload.channel_id)
        if channel is None:
            channel = await self.backend.fetch_channel(payload.channel_id)
        project = frontend.projects.for_channel(channel)
        if project is None or project.archived:
            return
        async with self.backend.events.event_locks.setdefault(session.codex_thread, asyncio.Lock()):
            if session.status == "ended" or session.discord_thread in self.backend.stopping:
                return
            entry = next(
                (e for e in session.prompt_messages if e["message"] == payload.message_id), None
            )
            if entry is None:
                return
            # Consume the user's click too, so their arrow does not remain next
            # to the bot's running/completed status and they can click again on failure.
            message = self.messages.get(entry["client_id"]) or channel.get_partial_message(
                payload.message_id
            )
            try:
                await message.remove_reaction(payload.emoji, discord.Object(id=payload.user_id))
            except discord.HTTPException:
                LOG.warning("Could not clear queue click on message %s", payload.message_id)
            if entry["emoji"] != "↪️":
                return
            try:
                await self.backend.ensure_live(session)
                turn = await self.backend.live.promote_queued(
                    session.codex_thread, entry["client_id"]
                )
            except (RpcError, OSError, asyncio.TimeoutError) as exc:
                LOG.warning("Could not promote queued prompt %s: %s", payload.message_id, exc)
                detail = (
                    str(exc)
                    if isinstance(exc, RpcError)
                    else (
                        "Delivery could not be confirmed. Check this thread before resending; "
                        "the prompt may already be running."
                    )
                )
                await self.backend.say(await self.backend.live_channel(session), detail)
                return
            if turn:
                entry.update(turn=turn["id"], emoji="👀")
                if turn["status"] != "inProgress":
                    self.observe_turn(session, turn)
                await self.sync(session)

    def track(self, session, source, client_id):
        entry = {
            "client_id": client_id,
            "channel": source.channel.id,
            "message": source.id,
            "emoji": "↪️",
            "created": time.time(),
        }
        session.prompt_messages.append(entry)
        self.messages[client_id] = source
        self.backend.store.save()
        return entry

    def observe_item(self, session, turn_id, item):
        if item.get("type") != "userMessage" or not item.get("clientId"):
            return
        for entry in session.prompt_messages:
            if entry["client_id"] == item["clientId"]:
                entry["turn"] = turn_id
                if entry["emoji"] not in {"✅", "❌"}:
                    entry["emoji"] = "👀"

    def observe_turn(self, session, turn):
        for item in turn.get("items", []):
            self.observe_item(session, turn["id"], item)
        if turn["status"] != "inProgress":
            for entry in session.prompt_messages:
                if entry.get("turn") == turn["id"]:
                    entry["emoji"] = "✅" if turn["status"] == "completed" else "❌"

    async def sync(self, session):
        if not session.prompt_messages:
            return
        self.backend.store.save()
        for entry in list(session.prompt_messages):
            emoji = entry["emoji"]
            if entry.get("applied") != emoji:
                try:
                    message = self.messages.get(entry["client_id"])
                    if message is None:
                        channel = self.backend.get_channel(entry["channel"])
                        if channel is None:
                            channel = await self.backend.fetch_channel(entry["channel"])
                        message = channel.get_partial_message(entry["message"])
                    await message.add_reaction(emoji)
                    for old in EMOJI:
                        if old != emoji:
                            await message.remove_reaction(old, self.backend.frontend.user)
                except discord.NotFound:
                    session.prompt_messages.remove(entry)
                    self.messages.pop(entry["client_id"], None)
                    continue
                except (discord.HTTPException, OSError, asyncio.TimeoutError):
                    LOG.warning("Could not update prompt reaction %s; will retry", entry["message"])
                    continue
                entry["applied"] = emoji
            if emoji in {"✅", "❌"}:
                session.prompt_messages.remove(entry)
                self.messages.pop(entry["client_id"], None)
        self.backend.store.save()

    async def reconcile(self, session):
        if not session.prompt_messages:
            return
        async with self.backend.events.event_locks.setdefault(session.codex_thread, asyncio.Lock()):
            await self.sync(session)
            if not session.prompt_messages:
                return
            await self.backend.live.connect()
            queued, cursor = set(), None
            while True:
                page = await self.backend.live.call(
                    "thread/queue/list",
                    {
                        "threadId": session.codex_thread,
                        "cursor": cursor,
                        "limit": 100,
                    },
                )
                queued.update(row["clientUserMessageId"] for row in page["data"])
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            # History carries the same client IDs, including when another client
            # starts a queued message or a turn finishes while Chert is offline.
            missing = {e["client_id"] for e in session.prompt_messages} - queued
            if missing:
                oldest = min(e["created"] for e in session.prompt_messages)
                cursor = None
                while True:
                    page = await self.backend.live.call(
                        "thread/turns/list",
                        {
                            "threadId": session.codex_thread,
                            "cursor": cursor,
                            "limit": 20,
                            "sortDirection": "desc",
                            "itemsView": "summary",
                        },
                    )
                    past = False
                    for turn in page["data"]:
                        self.observe_turn(session, turn)
                        # Steered inputs may omit clientId in persisted history;
                        # the acknowledged turn ID is also authoritative.
                        missing.difference_update(
                            e["client_id"]
                            for e in session.prompt_messages
                            if e.get("turn") == turn["id"]
                        )
                        missing.difference_update(
                            i.get("clientId")
                            for i in turn.get("items", [])
                            if i.get("type") == "userMessage"
                        )
                        if (turn.get("startedAt") or time.time()) + 1 < oldest:
                            past = True
                    cursor = page.get("nextCursor")
                    if not missing or past or not cursor:
                        break
                # Allow native history to settle before marking removed queue
                # entries or ambiguous failed deliveries as unsuccessful.
                for entry in session.prompt_messages:
                    if entry["client_id"] in missing and time.time() - entry["created"] > 60:
                        entry["emoji"] = "❌"
            await self.sync(session)
