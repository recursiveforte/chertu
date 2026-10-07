"""Opt-in real-runtime check with isolated CODEX_HOME and an in-memory Discord sink.

Run on the deployment host: .venv/bin/python tests/live_codex_activity.py
Use --model to exercise the real /model command and subsequent inference instead.
Use --projects to check project launches, routing, and harness changes with a real
isolated Codex runtime and in-memory Discord channels.
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

from config import CodexOptions
from codex_backend import Session, SessionStore
from config import Config
from shared_frontend import SharedFrontend


async def check_model(frontend, session, channel, consumer):
    adapter = frontend.codex
    frontend.owner = adapter.owner = 7
    adapter.main_channel = SimpleNamespace(id=100)

    async def turn():
        await adapter.send_prompt(channel, 'Reply exactly OK. Do not use tools.')
        async with asyncio.timeout(120):
            while session.status not in {'idle', 'error', 'interrupted'}:
                if consumer.done():
                    await consumer
                await asyncio.sleep(.1)
        await adapter.live.notifications.join()
        assert session.status == 'idle', f'Inference failed: {session.status}'

    await turn()  # Materialize history, as for an existing Discord conversation.
    before = (await adapter.live.call('thread/read', {'threadId': session.codex_thread, 'includeTurns': False}))['thread']['model']
    models = (await adapter.live.call('model/list', {}))['data']
    target = next(m['model'] for m in models if m['model'] != before)
    interaction = SimpleNamespace(channel=channel, channel_id=channel.id, user=SimpleNamespace(id=7),
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    command = frontend.tree.get_command('model')
    arguments = await command._transform_arguments(interaction, SimpleNamespace())
    await command._do_call(interaction, arguments)
    picker = interaction.followup.send.call_args.kwargs['view']
    assert before in interaction.followup.send.call_args.args[0]
    picker.select._values = [target]
    interaction.message = SimpleNamespace(edit=AsyncMock())
    assert await picker.interaction_check(interaction)
    await picker.choose(interaction)
    print('Bare /model picker and selection: PASS', flush=True)
    print('Slash command:', interaction.followup.send.call_args.args[0], flush=True)
    assert session.pending_settings.get('model') == target, 'Model choice was lost before the next turn'
    await turn()
    actual = (await adapter.live.call('thread/read', {'threadId': session.codex_thread, 'includeTurns': False}))['thread']['model']
    assert actual == target, f'Slash command selected {target}, but runtime used {actual}'
    assert 'model' not in session.pending_settings
    print(f'Real /model and completed inference: PASS ({before} → {actual})', flush=True)


async def check_projects(root, config, consumer_factory):
    from projects import Project, ProjectStore
    from project_frontend import ProjectFrontend

    projects = ProjectStore(root / 'projects.json')
    projects.guild_id = 1
    projects.projects['work'] = Project('work', str(config.project_root), 100)
    projects.save()
    frontend = ProjectFrontend(config, CodexOptions(), SessionStore(config.state_file), projects=projects)
    frontend.owner = frontend.codex.owner = 7
    frontend._connection.user = SimpleNamespace(id=999)
    parent = SimpleNamespace(id=100, parent_id=None, edit=AsyncMock())
    parent.edit.return_value = parent
    thread = SimpleNamespace(id=300, parent_id=100, parent=parent, archived=False, mention='<#300>')
    source = SimpleNamespace(id=300, channel=parent, author=SimpleNamespace(id=7, bot=False),
        content='Reply exactly CHERT_PROJECT_OK. Do not use tools.', webhook_id=None,
        attachments=[], add_reaction=AsyncMock(), create_thread=AsyncMock(return_value=thread))
    frontend.project_channels[100] = parent
    frontend.main_channel = frontend.codex.main_channel = parent
    frontend.save_attachments = AsyncMock(return_value='')
    frontend.retitle = Mock()
    adapter = frontend.codex
    adapter.live_channel = AsyncMock(return_value=thread)
    messages = []
    async def post_as(channel, name, content, thread_id=None, **kwargs):
        assert channel is parent, 'Reply sent to a different project'
        messages.append(content)
        return SimpleNamespace(id=len(messages))
    frontend.post_as = post_as
    adapter.webhook_for = AsyncMock(return_value=SimpleNamespace(edit_message=AsyncMock()))
    await adapter.live.connect()
    consumer = asyncio.create_task(consumer_factory(adapter))
    try:
        await frontend.on_message(source)
        assert 300 in adapter.store.sessions, 'Project prompt did not create its session'
        session = adapter.store.sessions[300]
        async def completed():
            async with asyncio.timeout(120):
                while session.status not in {'idle', 'error', 'interrupted'}:
                    if consumer.done():
                        await consumer
                    await asyncio.sleep(.1)
            await adapter.live.notifications.join()
            assert session.status == 'idle', session.status
        await completed()
        assert any('CHERT_PROJECT_OK' in m for m in messages), 'Reply missing from project thread'
        native = (await adapter.live.call('thread/read', {'threadId': session.codex_thread,
                      'includeTurns': False}))['thread']
        assert Path(native['cwd']) == config.project_root
        source.create_thread.assert_awaited_once()
        print('Project prompt → correct native directory and attached Discord thread: PASS', flush=True)

        project = projects.projects['work']
        await frontend.set_harness(project, 'claude')
        assert frontend.backend_for(parent) == 'claude'
        assert frontend.backend_for(thread) == 'codex'
        assert ProjectStore(projects.path).projects['work'].harness == 'claude'
        reply = SimpleNamespace(channel=thread, author=source.author, webhook_id=None, attachments=[],
                                content='Reply exactly CHERT_EXISTING_CODEX_OK. Do not use tools.', add_reaction=AsyncMock())
        await frontend.on_message(reply)
        await completed()
        assert any('CHERT_EXISTING_CODEX_OK' in m for m in messages), 'Existing thread changed harness'
        print('Default harness change persists; existing thread still completes a real Codex turn: PASS', flush=True)
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await frontend.close()


async def main(model_check=False, project_check=False):
    binary = shutil.which('codex') or str(Path.home()/'.local/bin/codex')
    original_home = Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')
    with tempfile.TemporaryDirectory(prefix='chert-qa-', dir='/tmp') as temporary:
        root = Path(temporary)
        home = root/'home'
        home.mkdir(mode=0o700)
        shutil.copyfile(original_home/'auth.json', home/'auth.json')
        (home/'auth.json').chmod(0o600)
        project = root/'project'
        project.mkdir()
        socket = root/'server.sock'
        env = {k: v for k, v in os.environ.items() if not k.startswith(('DISCORD_', 'CODEX_'))}
        env['CODEX_HOME'] = str(home)
        log = (root/'server.log').open('wb')
        server = await asyncio.create_subprocess_exec(binary, 'app-server', '--listen', f'unix://{socket}',
            cwd=project, env=env, stdout=log, stderr=log, start_new_session=True)
        frontend = None
        try:
            async with asyncio.timeout(20):
                while not socket.exists():
                    if server.returncode is not None:
                        raise RuntimeError('Isolated app-server exited before opening its socket')
                    await asyncio.sleep(.1)
            config = Config('unused', 100, 7, set(), project, root/'state.json', discover=False, live_socket=socket)
            if project_check:
                async def consume_project(adapter):
                    while True:
                        event = await adapter.live.notifications.get()
                        try:
                            await adapter.handle_live_event(event)
                        finally:
                            adapter.live.notifications.task_done()
                await check_projects(root, config, consume_project)
                return
            frontend = SharedFrontend(config, CodexOptions(), SessionStore(config.state_file), 0)
            adapter = frontend.codex
            live = adapter.live
            await live.connect()
            response = await live.call('thread/start', {'cwd': str(project), 'approvalPolicy': 'on-request',
                'sandbox': 'workspace-write', 'ephemeral': not model_check})
            sid = response['thread']['id']
            live.subscribed.add(sid)
            session = Session(300, str(project), 'isolated activity verification', sid,
                              backend='app-server', native_settings=True, display_model=response.get('model', ''))
            adapter.store.sessions[300] = session
            channel = SimpleNamespace(id=300, archived=False)
            adapter.live_channel = AsyncMock(return_value=channel)
            frontend.retitle = Mock()
            messages, edits, events = [], [], []
            async def say(channel, text):
                messages.append(text)
                return SimpleNamespace(id=len(messages))
            async def edit(message_id, **kwargs):
                edits.append(kwargs['content'])
            adapter.say = say
            adapter.webhook_for = AsyncMock(return_value=SimpleNamespace(edit_message=edit))

            async def consume():
                while True:
                    event = await live.notifications.get()
                    events.append(event['method'])
                    await adapter.handle_live_event(event)
                    live.notifications.task_done()
            consumer = asyncio.create_task(consume())
            async def heartbeat():
                while True:
                    await adapter.activity_tick()
                    await asyncio.sleep(1)
            ticker = asyncio.create_task(heartbeat())
            try:
                if model_check:
                    await check_model(frontend, session, channel, consumer)
                    return
                started = time.monotonic()
                await adapter.send_prompt(channel,
                    'Run a shell command that sleeps for 23 seconds and then prints CHERT_ACTIVITY_TOOL_OK. '
                    'Do not modify any files. After the command finishes, reply exactly CHERT_ACTIVITY_DONE.')
                assert any('**exploring**' in m for m in messages), 'No immediate working card'
                print('Immediate working card: PASS', flush=True)
                async with asyncio.timeout(180):
                    while session.status not in {'idle', 'error', 'interrupted'}:
                        if consumer.done():
                            await consumer
                        await asyncio.sleep(.2)
                await live.notifications.join()
                assert session.status == 'idle', f'Turn did not complete: {session.status}'
                assert any('CHERT_ACTIVITY_DONE' in m for m in messages), 'Final reply missing'
                assert any('turn done' in m for m in edits), 'Completion card missing'
                assert session.activity.get('counts'), 'Tool progress missing'
                working = [m for m in edits if '**exploring**' in m]
                assert len(working) >= 2, 'No working-card heartbeat during command'
                assert '🔧' in '\n'.join(working), 'No tool count in visible card'
                print('Native tool progress, heartbeat, final reply and completion: PASS', flush=True)
                print(f'Observed {len(events)} native events, {len(edits)} card edits in {time.monotonic()-started:.1f}s', flush=True)
                print('Final card:', edits[-1], flush=True)
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


if __name__ == '__main__':
    asyncio.run(main(model_check='--model' in sys.argv, project_check='--projects' in sys.argv))
