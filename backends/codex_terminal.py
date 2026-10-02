"""An on-demand terminal client for an existing Codex daemon conversation."""
import asyncio
import os
import shlex
import shutil
from pathlib import Path

import discord_bot as upstream


class CodexTerminal:
    def __init__(self, binary, socket):
        self.binary, self.socket = binary, socket
        self.lock = asyncio.Lock()

    async def run(self, *args):
        env = os.environ.copy()
        env.pop('DISCORD_BOT_TOKEN', None)
        proc = await asyncio.create_subprocess_exec('tmux', *args, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), 10)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise
        return proc.returncode, out.decode(errors='replace').strip(), err.decode(errors='replace').strip()

    async def valid(self, session):
        if not session.terminal_pane:
            return False
        rc, text, _ = await self.run('display-message', '-p', '-t', session.terminal_pane,
                                   '#{@chert_codex_sid}|#{pane_dead}|#{pane_start_command}')
        fields = text.split('|', 2)
        if rc or len(fields) != 3 or fields[0] != session.codex_thread or fields[1] != '0':
            return False
        try:
            actual = shlex.split(fields[2])
            expected = shlex.split(session.terminal_command)
        except ValueError:
            return False
        return fields[2] == session.terminal_command or actual in ([session.terminal_command], expected)

    async def ensure(self, session):
        if not shutil.which('tmux'):
            raise ValueError('Install tmux to use the terminal view.')
        async with self.lock:
            if await self.valid(session):
                return session.terminal_pane
            tmux_session = upstream.TMUX_SESSION
            rc, _, _ = await self.run('has-session', '-t', tmux_session)
            if rc:
                rc, _, error = await self.run('new-session', '-d', '-s', tmux_session)
                if rc:
                    raise ValueError(error)
            command = 'exec ' + shlex.join([self.binary, '--remote', f'unix://{self.socket}',
                                            '-c', 'check_for_update_on_startup=false',
                                            '--no-alt-screen', 'resume', session.codex_thread])
            session.terminal_command = command
            rc, pane, error = await self.run('new-window', '-d', '-P', '-F', '#{pane_id}',
                '-t', tmux_session + ':', '-n', 'codex-' + session.name[:24], '-c', session.cwd,
                '-e', 'PATH=' + os.environ.get('PATH', '/usr/bin:/bin'),
                '-e', 'CODEX_HOME=' + os.environ.get('CODEX_HOME', str(Path.home()/'.codex')), command)
            if rc:
                raise ValueError(error)
            session.terminal_pane = pane
            await self.run('set-option', '-p', '-t', pane, '@chert_codex_sid', session.codex_thread)
            for _ in range(20):
                if await self.valid(session):
                    return pane
                await asyncio.sleep(0.25)
            raise ValueError('The Codex terminal client did not start. Check that the CLI supports --remote.')

    async def screen(self, session, ansi=False):
        pane = await self.ensure(session)
        args = ['capture-pane', '-p', '-t', pane]
        if ansi:
            args.append('-e')
        rc, text, error = await self.run(*args)
        if rc:
            raise ValueError(error)
        return text

    async def key(self, session, key):
        if key not in upstream.checkin.ALLOWED_KEYS:
            raise ValueError('Unsupported terminal key.')
        pane = await self.ensure(session)
        rc, _, error = await self.run('send-keys', '-t', pane, key)
        if rc:
            raise ValueError(error)

    async def close(self, session):
        if await self.valid(session):
            await self.run('kill-pane', '-t', session.terminal_pane)
        session.terminal_pane = None
