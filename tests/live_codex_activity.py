"""Opt-in real-runtime check with isolated CODEX_HOME and an in-memory Discord sink.

Run on the deployment host: .venv/bin/python tests/live_codex_activity.py
Use --model to exercise the real /model command and subsequent inference instead.
Use --projects to check project launches, routing, and harness changes with a real
isolated Codex runtime and in-memory Discord channels.
Use --close to verify native closure without resuming history.
Use --worktrees to check project defaults and both workspace overrides with real Git and Codex.
Use --queue to verify native queuing and Discord prompt reactions.
Use --queue-steer to promote a queued prompt by clicking its reaction during a real turn.
Use --rapid-queue to verify five quick replies leave one completed card per turn.
No Discord token, production daemon, or production conversation is used.
"""

import asyncio
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chert.config import CodexOptions
from chert.backends.codex.state import Session, SessionStore
from chert.config import Config
from support import make_frontend


async def check_model(frontend, session, channel, consumer):
    adapter = frontend.codex
    frontend.owner = adapter.owner = 7
    adapter.main_channel = SimpleNamespace(id=100)

    async def turn():
        previous_turns = session.turns
        await adapter.send_prompt(channel, "Reply exactly OK. Do not use tools.")
        async with asyncio.timeout(120):
            while session.turns == previous_turns and session.status not in {"error", "interrupted"}:
                if consumer.done():
                    await consumer
                await asyncio.sleep(0.1)
        await adapter.live.notifications.join()
        assert session.status == "idle", f"Inference failed: {session.status}"

    await turn()  # Materialize history, as for an existing Discord conversation.
    before = (
        await adapter.live.call(
            "thread/read", {"threadId": session.codex_thread, "includeTurns": False}
        )
    )["thread"]["model"]
    models = (await adapter.live.call("model/list", {}))["data"]
    target = next(m["model"] for m in models if m["model"] != before)
    interaction = SimpleNamespace(
        channel=channel,
        channel_id=channel.id,
        user=SimpleNamespace(id=7),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    command = frontend.tree.get_command("model")
    arguments = await command._transform_arguments(interaction, SimpleNamespace())
    await command._do_call(interaction, arguments)
    picker = interaction.followup.send.call_args.kwargs["view"]
    assert before in interaction.followup.send.call_args.args[0]
    picker.select._values = [target]
    interaction.message = SimpleNamespace(edit=AsyncMock())
    assert await picker.interaction_check(interaction)
    await picker.choose(interaction)
    print("Bare /model picker and selection: PASS", flush=True)
    print("Slash command:", interaction.followup.send.call_args.args[0], flush=True)
    assert session.pending_settings.get("model") == target, (
        "Model choice was lost before the next turn"
    )
    await turn()
    actual = (
        await adapter.live.call(
            "thread/read", {"threadId": session.codex_thread, "includeTurns": False}
        )
    )["thread"]["model"]
    assert actual == target, f"Slash command selected {target}, but runtime used {actual}"
    assert "model" not in session.pending_settings
    print(f"Real /model and completed inference: PASS ({before} → {actual})", flush=True)


async def check_close(frontend, session, channel, consumer):
    adapter = frontend.codex
    previous_turns = session.turns
    await adapter.send_prompt(channel, "Reply exactly OK. Do not use tools.")
    async with asyncio.timeout(120):
        while session.turns == previous_turns and session.status not in {"error", "interrupted"}:
            if consumer.done():
                await consumer
            await asyncio.sleep(0.1)
    await adapter.live.notifications.join()
    assert session.status == "idle", f"Inference failed: {session.status}"
    channel.edit = AsyncMock()
    adapter.live.subscribed.clear()
    adapter.live.attach = AsyncMock(side_effect=AssertionError("Close must not resume history"))
    result = await adapter.stop(channel, end=True)
    assert result == "Session ended and Discord thread closed.", result
    channel.edit.assert_awaited_once_with(archived=True)
    assert session.status == "ended"
    loaded = await adapter.live.loaded_threads()
    assert session.codex_thread not in {t["id"] for t in loaded}, "Actor is still loaded"
    channel.archived = True
    session.activity["last_edit"] = 0
    session.subagents["dirty"] = True
    await adapter.live.notifications.join()
    adapter.live_channel.reset_mock()
    for _ in range(3):
        await adapter.events.activity_tick()
        await adapter.discover_once()
    channel.edit.assert_awaited_once_with(archived=True)
    adapter.live_channel.assert_not_called()
    assert session.status == "ended"
    print(
        "Native close without history attachment; Discord archive payload (in-memory): PASS",
        flush=True,
    )
    print(
        "Closed session stays archived across activity and discovery ticks (in-memory): PASS",
        flush=True,
    )


async def check_queue(frontend, session, channel, consumer, promote=False):
    adapter = frontend.codex
    frontend._connection.user = SimpleNamespace(id=999)
    adapter.get_channel = lambda _: channel
    sources, transitions, visible = {}, {}, {}
    for ident in (501, 502, 503):
        transitions[ident], visible[ident] = [], set()

        async def add(emoji, ident=ident):
            transitions[ident].append(emoji)
            visible[ident].add(emoji)

        async def remove(emoji, user, ident=ident):
            assert user.id in {999, 7}
            visible[ident].discard(emoji)

        sources[ident] = SimpleNamespace(
            id=ident, channel=channel, add_reaction=add, remove_reaction=remove
        )
    await adapter.send_prompt(
        channel,
        "Run a shell command that sleeps 8 seconds, then reply exactly QUEUE_FIRST_DONE.",
        source=sources[501],
    )
    async with asyncio.timeout(30):
        while "👀" not in visible[501] or session.active_turn is None:
            if consumer.done():
                await consumer
            await asyncio.sleep(0.1)
    for ident in (502, 503):
        await adapter.send_prompt(
            channel, f"Reply exactly QUEUE_{ident}_DONE. Do not use tools.", source=sources[ident]
        )
        assert visible[ident] == {"↪️"}, visible
    queue = await adapter.live.call("thread/queue/list", {"threadId": session.codex_thread})
    assert [q["clientUserMessageId"] for q in queue["data"]] == ["chert:502", "chert:503"]
    print("Follow-ups present in the native Codex queue: PASS", flush=True)
    if promote:
        active = await adapter.live.active_turn(session.codex_thread)
        await frontend.on_raw_reaction_add(
            SimpleNamespace(
                message_id=503,
                channel_id=channel.id,
                user_id=7,
                guild_id=1,
                emoji="↪️",
                member=SimpleNamespace(bot=False),
            )
        )
        assert visible[503] == {"👀"}, transitions
        entry = next(e for e in session.prompt_messages if e["message"] == 503)
        assert entry["turn"] == active, (entry["turn"], active)
        queue = await adapter.live.call("thread/queue/list", {"threadId": session.codex_thread})
        assert [q["clientUserMessageId"] for q in queue["data"]] == ["chert:502"]
        print(
            "Raw reaction click → selected native entry removed and steered into the active turn: PASS",
            flush=True,
        )
    async with asyncio.timeout(180):
        while session.prompt_messages:
            if consumer.done():
                await consumer
            await asyncio.sleep(0.1)
    await adapter.live.notifications.join()
    for ident in (501, 502, 503):
        assert visible[ident] == {"✅"}, (ident, transitions)
        assert transitions[ident][-2:] == ["👀", "✅"], transitions
    page = await adapter.live.call(
        "thread/turns/list",
        {
            "threadId": session.codex_thread,
            "limit": 10,
            "itemsView": "summary",
            "sortDirection": "asc",
        },
    )
    turns = page["data"]
    assert len(turns) == (2 if promote else 3) and all(t["status"] == "completed" for t in turns), (
        turns
    )
    ids = [[i.get("clientId") for i in t["items"] if i["type"] == "userMessage"] for t in turns]
    if promote:
        assert "chert:501" in ids[0] and ids[-1] == ["chert:502"], ids
        assert any(
            "QUEUE_503_DONE" in i.get("text", "")
            for i in turns[0]["items"]
            if i["type"] == "agentMessage"
        )
        print(
            "Promoted prompt affected the active response, reached ✅, and did not run twice: PASS",
            flush=True,
        )
        return
    assert ids == [["chert:501"], ["chert:502"], ["chert:503"]], ids
    print(
        "Three separate completed turns in FIFO order; ↪️ → 👀 → ✅ (in-memory Discord): PASS",
        flush=True,
    )

    # Reconstruct the reaction state as if the bridge had missed every event.
    from chert.backends.codex.reactions import PromptReactions

    session.prompt_messages = [
        {
            "client_id": "chert:503",
            "message": 503,
            "channel": channel.id,
            "emoji": "↪️",
            "applied": "↪️",
            "created": time.time() - 180,
        }
    ]
    adapter.store.save()
    adapter.store = SessionStore(adapter.store.path)
    adapter.reactions = PromptReactions(adapter)
    adapter.get_channel = lambda _: channel
    channel.get_partial_message = lambda ident: sources[ident]
    await adapter.reactions.reconcile(adapter.store.sessions[channel.id])
    assert not adapter.store.sessions[channel.id].prompt_messages
    assert transitions[503][-1] == "✅"
    print(
        "Restart reconciliation from native history/client ID, without resubmission: PASS",
        flush=True,
    )


async def check_rapid_queue(frontend, session, channel, consumer):
    adapter = frontend.codex
    frontend._connection.user = SimpleNamespace(id=999)
    rendered, cards, sources = {}, [], []

    async def say(destination, text):
        assert destination is channel
        await asyncio.sleep(0.15)  # Discord delivery lags behind the socket reader.
        ident = len(rendered) + 1
        rendered[ident] = text
        if "**working**" in text:
            cards.append(ident)
        return SimpleNamespace(id=ident)

    async def edit(ident, **kwargs):
        await asyncio.sleep(0.15)
        assert ident in rendered
        rendered[ident] = kwargs["content"]

    adapter.say = say
    adapter.webhook_for = AsyncMock(return_value=SimpleNamespace(edit_message=edit))
    for ident in range(1, 6):
        sources.append(SimpleNamespace(
            id=500 + ident, channel=channel, author=SimpleNamespace(id=7),
            content=f"Reply exactly RAPID_REPLY_{ident}. Do not use tools.",
            webhook_id=None, attachments=[], add_reaction=AsyncMock(), remove_reaction=AsyncMock(),
            create_thread=AsyncMock(side_effect=AssertionError("Must keep existing thread")),
        ))

    async def discover():
        while True:
            info = (await adapter.live.call(
                "thread/read", {"threadId": session.codex_thread, "includeTurns": False}
            ))["thread"]
            await adapter.events.observe_status(session, info)
            await asyncio.sleep(0.1)

    poller = None
    try:
        await asyncio.gather(*(frontend.on_message(source) for source in sources))
        poller = asyncio.create_task(discover())
        async with asyncio.timeout(180):
            while session.turns < 5 or session.prompt_messages:
                for task in (consumer, poller):
                    if task.done():
                        await task
                await asyncio.sleep(0.1)
        await adapter.live.notifications.join()
        assert session.status == "idle", session.status
        assert len(adapter.store.sessions) == 1
        assert len(cards) == 5, f"Expected five cards, got {len(cards)}: {rendered}"
        assert all("✅ turn done" in rendered[ident] for ident in cards), rendered
        assert all("**working**" not in text for text in rendered.values()), rendered
        for ident, source in enumerate(sources, 1):
            assert sum(text.strip() == f"RAPID_REPLY_{ident}" for text in rendered.values()) == 1
            source.add_reaction.assert_awaited_with("✅")
            source.create_thread.assert_not_called()
        page = await adapter.live.call(
            "thread/turns/list",
            {"threadId": session.codex_thread, "limit": 10,
             "sortDirection": "asc", "itemsView": "summary"},
        )
        assert len(page["data"]) == 5
        assert [
            item.get("clientId") for turn in page["data"] for item in turn["items"]
            if item["type"] == "userMessage"
        ] == [f"chert:{source.id}" for source in sources]
        print("Five rapid prompts in one native thread: FIFO replies and completion reactions PASS",
              flush=True)
        print("Slow Discord + concurrent discovery: exactly five finalized cards, no duplicate replies PASS",
              flush=True)
    finally:
        if poller:
            poller.cancel()
            await asyncio.gather(poller, return_exceptions=True)


async def check_projects(root, config, consumer_factory, worktrees=False):
    from chert.projects import Project, ProjectStore
    from chert.discord.frontend import Frontend

    projects = ProjectStore(root / "projects.json")
    projects.guild_id = 1
    projects.projects["work"] = Project("work", str(config.project_root), 100)
    projects.save()
    frontend = Frontend(config, CodexOptions(), SessionStore(config.state_file), projects=projects)
    frontend.owner = frontend.codex.owner = 7
    frontend._connection.user = SimpleNamespace(id=999)
    parent = SimpleNamespace(id=100, parent_id=None, edit=AsyncMock())
    parent.edit.return_value = parent
    thread = SimpleNamespace(id=300, parent_id=100, parent=parent, archived=False, mention="<#300>")
    source = SimpleNamespace(
        id=300,
        channel=parent,
        author=SimpleNamespace(id=7, bot=False),
        content="Reply exactly CHERT_PROJECT_OK. Do not use tools.",
        webhook_id=None,
        attachments=[],
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        create_thread=AsyncMock(return_value=thread),
    )
    frontend.project_channels[100] = parent
    frontend.main_channel = frontend.codex.main_channel = parent
    frontend.save_attachments = AsyncMock(return_value="")
    frontend.retitle = Mock()
    adapter = frontend.codex
    adapter.live_channel = AsyncMock(return_value=thread)
    messages = []

    async def post_as(channel, name, content, thread_id=None, **kwargs):
        assert channel is parent, "Reply sent to a different project"
        messages.append(content)
        return SimpleNamespace(id=len(messages))

    frontend.post_as = post_as
    adapter.webhook_for = AsyncMock(return_value=SimpleNamespace(edit_message=AsyncMock()))
    await adapter.live.connect()
    consumer = asyncio.create_task(consumer_factory(adapter))
    try:
        if worktrees:
            from test_worktrees import repository

            repository(config.project_root)
            await frontend.project_commands.set_worktrees(projects.projects["work"], True)
        await frontend.on_message(source)
        assert 300 in adapter.store.sessions, "Project prompt did not create its session"
        session = adapter.store.sessions[300]

        completed_turns = {}

        async def completed():
            previous = completed_turns.get(session.codex_thread, 0)
            async with asyncio.timeout(120):
                while session.turns == previous and session.status not in {"error", "interrupted"}:
                    if consumer.done():
                        await consumer
                    await asyncio.sleep(0.1)
            await adapter.live.notifications.join()
            assert session.status == "idle", session.status
            completed_turns[session.codex_thread] = session.turns

        await completed()
        assert any("CHERT_PROJECT_OK" in m for m in messages), "Reply missing from project thread"
        native = (
            await adapter.live.call(
                "thread/read", {"threadId": session.codex_thread, "includeTurns": False}
            )
        )["thread"]
        if worktrees:
            assert Path(native["cwd"]) != config.project_root
            assert (Path(native["cwd"]) / "tracked.txt").read_text() == "committed"
            assert projects.for_directory(native["cwd"]) is projects.projects["work"]
        else:
            assert Path(native["cwd"]) == config.project_root
        source.create_thread.assert_awaited_once()
        print(
            "Project prompt → correct native directory and attached Discord thread: PASS",
            flush=True,
        )

        project = projects.projects["work"]
        await frontend.project_commands.set_harness(project, "claude")
        assert frontend.backend_for(parent) == "claude"
        assert frontend.backend_for(thread) == "codex"
        assert ProjectStore(projects.path).projects["work"].harness == "claude"
        reply = SimpleNamespace(
            id=301,
            channel=thread,
            author=source.author,
            webhook_id=None,
            attachments=[],
            content="Reply exactly CHERT_EXISTING_CODEX_OK. Do not use tools.",
            add_reaction=AsyncMock(),
            remove_reaction=AsyncMock(),
        )
        await frontend.on_message(reply)
        await completed()
        assert any("CHERT_EXISTING_CODEX_OK" in m for m in messages), (
            "Existing thread changed harness"
        )
        print(
            "Default harness change persists; existing thread still completes a real Codex turn: PASS",
            flush=True,
        )
        if worktrees:
            await frontend.project_commands.set_harness(project, "codex")
            original_cwd = session.cwd
            for name, enabled, identifier in (("no-worktree", True, 301), ("worktree", False, 302)):
                await frontend.project_commands.set_worktrees(project, enabled)
                thread = SimpleNamespace(
                    id=identifier,
                    parent_id=100,
                    parent=parent,
                    archived=False,
                    mention=f"<#{identifier}>",
                )
                parent.create_thread = AsyncMock(return_value=thread)
                adapter.live_channel = AsyncMock(return_value=thread)
                interaction = SimpleNamespace(
                    channel=parent,
                    user=source.author,
                    response=SimpleNamespace(defer=AsyncMock()),
                    followup=SimpleNamespace(send=AsyncMock()),
                )
                await frontend.tree.get_command(name)._do_call(
                    interaction, {"prompt": "Reply exactly CHERT_WORKTREE_OK. Do not use tools."}
                )
                session = adapter.store.sessions[identifier]
                await completed()
                native = (
                    await adapter.live.call(
                        "thread/read", {"threadId": session.codex_thread, "includeTurns": False}
                    )
                )["thread"]
                assert (Path(native["cwd"]) == config.project_root) == (name == "no-worktree")
                assert native["cwd"] == session.cwd
                assert projects.for_directory(session.cwd) is project
                assert project.worktrees == enabled
                assert adapter.store.sessions[300].cwd == original_cwd
                print(
                    f"/{name} override → correct native cwd and completed inference: PASS",
                    flush=True,
                )
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await frontend.close()


async def main(
    model_check=False,
    project_check=False,
    close_check=False,
    worktree_check=False,
    queue_check=False,
    queue_steer_check=False,
    rapid_queue_check=False,
):
    binary = shutil.which("codex") or str(Path.home() / ".local/bin/codex")
    original_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    with tempfile.TemporaryDirectory(prefix="chert-qa-", dir="/tmp") as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir(mode=0o700)
        shutil.copyfile(original_home / "auth.json", home / "auth.json")
        (home / "auth.json").chmod(0o600)
        project = root / "project"
        project.mkdir()
        socket = root / "server.sock"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DISCORD_", "CODEX_"))}
        env["CODEX_HOME"] = str(home)
        log = (root / "server.log").open("wb")
        server = await asyncio.create_subprocess_exec(
            binary,
            "app-server",
            "--listen",
            f"unix://{socket}",
            cwd=project,
            env=env,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        frontend = None
        try:
            async with asyncio.timeout(20):
                while not socket.exists():
                    if server.returncode is not None:
                        raise RuntimeError("Isolated app-server exited before opening its socket")
                    await asyncio.sleep(0.1)
            config = Config(
                "unused",
                1,
                7,
                set(),
                project,
                root / "state.json",
                discover=False,
                live_socket=socket,
            )
            if project_check or worktree_check:

                async def consume_project(adapter):
                    while True:
                        event = await adapter.live.notifications.get()
                        try:
                            await adapter.events.handle_live_event(event)
                        finally:
                            adapter.live.notifications.task_done()

                await check_projects(root, config, consume_project, worktrees=worktree_check)
                return
            frontend = make_frontend(config, CodexOptions(), SessionStore(config.state_file), 0)
            adapter = frontend.codex
            live = adapter.live
            await live.connect()
            response = await live.call(
                "thread/start",
                {
                    "cwd": str(project),
                    "approvalPolicy": "on-request",
                    "sandbox": "workspace-write",
                    # Native queued submissions need persisted history; the
                    # entire isolated home is removed after every check.
                    "ephemeral": False,
                },
            )
            sid = response["thread"]["id"]
            live.subscribed.add(sid)
            session = Session(
                300,
                str(project),
                "isolated activity verification",
                sid,
                backend="app-server",
                native_settings=True,
                display_model=response.get("model", ""),
            )
            adapter.store.sessions[300] = session
            channel = SimpleNamespace(
                id=300, parent_id=100, parent=frontend.project_channels[100], archived=False
            )
            adapter.live_channel = AsyncMock(return_value=channel)
            frontend.retitle = Mock()
            messages, edits, events = [], [], []

            async def say(channel, text):
                messages.append(text)
                return SimpleNamespace(id=len(messages))

            async def edit(message_id, **kwargs):
                edits.append(kwargs["content"])

            adapter.say = say
            adapter.webhook_for = AsyncMock(return_value=SimpleNamespace(edit_message=edit))

            async def consume():
                while True:
                    event = await live.notifications.get()
                    events.append(event["method"])
                    await adapter.events.handle_live_event(event)
                    live.notifications.task_done()

            consumer = asyncio.create_task(consume())

            async def heartbeat():
                while True:
                    await adapter.events.activity_tick()
                    await asyncio.sleep(1)

            ticker = asyncio.create_task(heartbeat())
            try:
                if rapid_queue_check:
                    await check_rapid_queue(frontend, session, channel, consumer)
                    return
                if queue_check or queue_steer_check:
                    await check_queue(
                        frontend, session, channel, consumer, promote=queue_steer_check
                    )
                    return
                if close_check:
                    await check_close(frontend, session, channel, consumer)
                    return
                if model_check:
                    await check_model(frontend, session, channel, consumer)
                    return
                started = time.monotonic()
                await adapter.send_prompt(
                    channel,
                    "Run a shell command that sleeps for 23 seconds and then prints CHERT_ACTIVITY_TOOL_OK. "
                    "Do not modify any files. After the command finishes, reply exactly CHERT_ACTIVITY_DONE.",
                )
                async with asyncio.timeout(10):
                    while not messages:
                        if consumer.done():
                            await consumer
                        await asyncio.sleep(0.01)
                assert any("**working**" in m for m in messages), "No immediate working card"
                print("Immediate working card: PASS", flush=True)
                async with asyncio.timeout(180):
                    while not session.turns and session.status not in {"error", "interrupted"}:
                        if consumer.done():
                            await consumer
                        await asyncio.sleep(0.2)
                await live.notifications.join()
                assert session.status == "idle", f"Turn did not complete: {session.status}"
                assert any("CHERT_ACTIVITY_DONE" in m for m in messages), "Final reply missing"
                assert any("turn done" in m for m in edits), "Completion card missing"
                assert session.activity.get("counts"), "Tool progress missing"
                working = [m for m in edits if "**working**" in m]
                assert len(working) >= 2, "No working-card heartbeat during command"
                assert "🔧" in "\n".join(working), "No tool count in visible card"
                print(
                    "Native tool progress, heartbeat, final reply and completion: PASS", flush=True
                )
                print(
                    f"Observed {len(events)} native events, {len(edits)} card edits in {time.monotonic() - started:.1f}s",
                    flush=True,
                )
                print("Final card:", edits[-1], flush=True)
            finally:
                consumer.cancel()
                ticker.cancel()
                await asyncio.gather(consumer, ticker, return_exceptions=True)
        finally:
            if frontend:
                await frontend.close()
            if server.returncode is None:
                os.killpg(server.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(server.wait(), 10)
                except TimeoutError:
                    os.killpg(server.pid, signal.SIGKILL)
                    await server.wait()
            log.close()


if __name__ == "__main__":
    asyncio.run(
        main(
            model_check="--model" in sys.argv,
            project_check="--projects" in sys.argv,
            close_check="--close" in sys.argv,
            worktree_check="--worktrees" in sys.argv,
            queue_check="--queue" in sys.argv,
            queue_steer_check="--queue-steer" in sys.argv,
            rapid_queue_check="--rapid-queue" in sys.argv,
        )
    )
