"""Opt-in real-runtime check with isolated CODEX_HOME and an in-memory Discord sink.

Run on the deployment host: .venv/bin/python tests/live_codex_activity.py
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

from codex_backend import CodexRunner, Session, SessionStore
from codex_bot import Config
from codex_live import LiveCodex
from shared_frontend import SharedFrontend


async def main():
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
            frontend = SharedFrontend(config, CodexRunner(), SessionStore(config.state_file), 0)
            adapter = frontend.codex
            live = adapter.live
            await live.connect()
            response = await live.call('thread/start', {'cwd': str(project), 'approvalPolicy': 'on-request',
                'sandbox': 'workspace-write', 'ephemeral': True})
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
    asyncio.run(main())
