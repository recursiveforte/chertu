"""One Discord gateway and command surface, using upstream Chert as the baseline.

discord_bot.py remains byte-for-byte upstream. Claude runs its original hooks,
poller, controls, and recovery. Codex attaches a controller to this gateway; it
never logs in separately or registers a competing set of slash commands.
"""
import asyncio
import functools
import logging
import os
from pathlib import Path

import discord

import discord_bot as upstream
from codex_backend import CodexRunner, SessionStore
from codex_bot import Config

LOG = logging.getLogger(__name__)
UPSTREAM_COMMIT = '0bd0902468437823863c2a38ddd5c7e01c9a61bd'


class SharedFrontend(upstream.Bridge):
    def __init__(self, codex_config, runner, store, claude_channel_id):
        self.claude_channel_id = claude_channel_id
        self.claude_enabled = bool(claude_channel_id) and os.environ.get('CLAUDE_ENABLED', '1') != '0'
        upstream.CHANNEL_ID = claude_channel_id
        upstream.PROJECT_ROOT = codex_config.project_root
        super().__init__(intents=self.intents_for_bot())
        from backends.codex import CodexChannel
        from backends.claude import ClaudeBackend
        self.codex = CodexChannel(codex_config, runner, store, self)
        self.claude = ClaudeBackend(self)
        self.original_commands = {c.name: c.callback for c in self.tree.get_commands()}
        self.install_routes()

    @staticmethod
    def intents_for_bot():
        intents = discord.Intents.default()
        intents.message_content = True
        return intents

    def backend_for(self, channel):
        channel_id = getattr(channel, 'id', None)
        if channel_id in self.codex.store.sessions:
            return 'codex'
        if channel_id in upstream.thread_to_key():
            return 'claude'
        parent = getattr(channel, 'parent_id', None) or getattr(channel, 'id', None)
        if parent == self.codex.config.channel_id:
            return 'codex'
        if parent and parent in {getattr(self.codex.broadcast_channel, 'id', None), getattr(self.codex.chat_channel, 'id', None)}:
            return 'codex'
        if parent == self.claude_channel_id:
            return 'claude'
        if parent in {getattr(self.chat_channel, 'id', None), getattr(self.broadcast_channel, 'id', None)}:
            return 'claude' if parent else None
        return None

    async def setup_hook(self):
        # Attach transports only after the host's asyncio/HTTP state is initialized.
        self.codex.bind_gateway()
        await self.codex.start_backend()
        self.owner = self.codex.owner
        if self.claude_channel_id:
            self.main_channel = await self.fetch_channel(self.claude_channel_id)
            if self.main_channel.guild.id != self.codex.main_channel.guild.id:
                raise ValueError('#claude and #codex must be in the same Discord server.')
            await self.claude.start()
        else:
            self.owner = self.codex.owner
            await self.start_hook_server()
            self.tree.copy_global_to(guild=self.codex.main_channel.guild)
            await self.tree.sync(guild=self.codex.main_channel.guild)

    async def start_hook_server(self):
        # Keep upstream's local endpoints and add a backend-aware dashboard sender.
        from aiohttp import web
        app = web.Application(client_max_size=1_000_000)
        app.router.add_post('/hook', self.hook_http)
        app.router.add_post('/admin/restart-all', self.admin_restart_all)
        app.router.add_post('/session-file', self.session_file_http)
        app.router.add_post('/codex/send', self.codex_send_http)
        app.router.add_post('/codex/screen', self.codex_screen_http)
        app.router.add_get('/health', lambda request: web.Response(text='ok\n'))
        self._hook_runner = web.AppRunner(app, access_log=None)
        await self._hook_runner.setup()
        await web.TCPSite(self._hook_runner, '127.0.0.1', upstream.HOOK_PORT).start()

    async def codex_send_http(self, request):
        from aiohttp import web
        secret = self.hook_secret()
        if not secret or request.headers.get('X-Hearth-Secret') != secret:
            return web.json_response({'error': 'bad secret'}, status=403)
        body = await request.json()
        session = next((s for s in self.codex.store.sessions.values() if s.codex_thread == body.get('sid')), None)
        if session is None:
            return web.json_response({'error': 'session not found'}, status=404)
        if session.status == 'ended':
            return web.json_response({'error': 'session ended; revive it first'}, status=409)
        channel = await self.codex.live_channel(session)
        key = body.get('key')
        if key in {'Escape', 'esc'} and not session.terminal_pane:
            await self.codex.stop(channel)
        elif key:
            await self.codex.terminal.key(session, key)
            self.codex.store.save()
        else:
            text = (body.get('text') or '').strip()
            if not text or len(text) > upstream.checkin.MAX_SEND_LEN:
                return web.json_response({'error': 'empty or oversized message'}, status=400)
            await self.codex.send_prompt(channel, text)
        return web.json_response({'ok': True})

    async def codex_screen_http(self, request):
        from aiohttp import web
        secret = self.hook_secret()
        if not secret or request.headers.get('X-Hearth-Secret') != secret:
            return web.json_response({'error': 'bad secret'}, status=403)
        body = await request.json()
        session = next((s for s in self.codex.store.sessions.values() if s.codex_thread == body.get('sid')), None)
        if session is None:
            return web.json_response({'error': 'session not found'}, status=404)
        if session.status == 'ended':
            return web.json_response({'error': 'session ended; revive it first'}, status=409)
        await self.codex.ensure_live(session)
        text = await self.codex.terminal.screen(session, ansi=True)
        self.codex.store.save()
        return web.json_response({'html': upstream.checkin.ansi_to_html(text),
                                  'status': session.status, 'name': session.name})

    async def session_file_http(self, request):
        from aiohttp import web
        secret = self.hook_secret()
        if not secret or request.headers.get('X-Hearth-Secret') != secret:
            return web.Response(status=403, text='bad secret\n')
        body = await request.json()
        path = body.get('path') or ''
        if not Path(path).is_file():
            return web.Response(status=404, text='file not found\n')
        try:
            await self.deliver_session_file(path, body.get('caption') or '', body.get('pane'), body.get('sid'), body.get('cwd'), body.get('backend'))
        except ValueError as exc:
            return web.Response(status=409, text=str(exc))
        return web.Response(status=202, text='sent\n')

    async def on_ready(self):
        LOG.info('Shared Chert frontend online: #codex=%s #claude=%s',
                 self.codex.config.channel_id, self.claude_channel_id)

    async def webhook_for(self, channel):
        # Keep edit access to messages posted by the pre-audit Codex fork.
        if channel.id == self.codex.config.channel_id and channel.id not in self.webhooks:
            hooks = await channel.webhooks()
            legacy = next((w for w in hooks if w.name == 'chert-codex' and w.token), None)
            if legacy:
                self.webhooks[channel.id] = legacy
                await self.apply_avatar(legacy)
        return await super().webhook_for(channel)

    async def board_text(self):
        return await self.board_for('claude')

    async def board_for(self, backend):
        if backend == 'claude':
            sessions = await asyncio.to_thread(upstream.live_sessions)
            state, meta, dashboard = upstream.state, upstream.state['_meta'], upstream.DASHBOARD
        else:
            sessions, state = [], {}
            meta = self.codex.store.meta
            dashboard = upstream.DASHBOARD.removesuffix('/claudes') + '/codex'
            for session in self.codex.store.sessions.values():
                if session.status in {'ended', 'disconnected'}:
                    continue
                status = 'busy' if session.status == 'running' else session.status
                sessions.append({'key': session.codex_thread, 'name': session.name,
                    'project': Path(session.cwd).name, 'status': status,
                    'updatedAt': max(session.turn_started, session.last_completed_at) * 1000,
                    'pane': None, 'sock': session.codex_thread})
                state[session.codex_thread] = {'thread': session.discord_thread}
        # Same signalscope layout as upstream; only the normalized records differ.
        free = f' · 💾 {upstream.ash_twin.disk_free()[0]:.1f} GB free' if upstream.ash_twin else ''
        lines = [f'🔭 **signalscope board** · {len(sessions)} traveler{"s" if len(sessions) != 1 else ""}'
                 f'{free} · [dashboard]({dashboard})']
        order = {'waiting': 0, 'busy': 1, 'shell': 2, 'idle': 3}
        for session in sorted(sessions, key=lambda s: (order.get(s['status'], 9), -s['updatedAt'])):
            saved = state.get(session['key'], {})
            where = f'<#{saved["thread"]}>' if saved.get('thread') else '*(thread opening)*'
            tag = {'pane': '', 'sock': ' 📨', None: ' 👁️'}[upstream.reach(session)]
            lines.append(f'{upstream.STATUS_EMOJI.get(session["status"], "⚪")} **{session["name"][:40]}** · '
                         f'`{session["project"]}` · {upstream.STATUS_WORD.get(session["status"], session["status"]).split(" —")[0]}{tag} · {where}')
        if meta.get('events'):
            lines.append('-# recent: ' + ' · '.join(f'{e["text"]} <t:{e["ts"]}:R>' for e in meta['events'][-5:]))
        import time
        body = '\n'.join(lines)[:1900]
        return body, body + f'\n-# updated <t:{int(time.time())}:R> · edits, never pings'

    async def update_shared_presence(self):
        claude_count = sum(not s.get('ended') for s in upstream.sessions_state().values())
        codex_count = sum(s.status not in {'ended', 'disconnected'} for s in self.codex.store.sessions.values())
        text = f'🔭 {claude_count + codex_count} travelers · Claude {claude_count} · Codex {codex_count}'
        if text != getattr(self, '_shared_presence', None):
            await discord.Client.change_presence(self, activity=discord.CustomActivity(text))
            self._shared_presence = text

    async def change_presence(self, **kwargs):
        await self.update_shared_presence()

    async def on_message(self, message):
        if message.author.id == self.user.id or message.webhook_id:
            return
        if not self.allowed_user(message.author):
            return
        backend = self.backend_for(message.channel)
        if backend == 'codex':
            try:
                return await self.codex.on_message(message)
            except Exception as exc:
                LOG.exception('Codex message handling failed')
                return await self.codex.say(message.channel, f'⚠️ {str(exc)[:1500]}')
        if backend == 'claude':
            return await self.claude.message(message)

    async def on_interaction(self, interaction):
        if interaction.type == discord.InteractionType.component and not self.allowed_user(interaction.user):
            if not interaction.response.is_done():
                await interaction.response.send_message('This Chert instance is restricted.', ephemeral=True)
            return
        if self.backend_for(interaction.channel) == 'codex':
            return await self.codex.on_component(interaction)
        return await super().on_interaction(interaction)

    def allowed_user(self, user):
        return user.id == (self.owner or self.codex.owner) or user.id in self.codex.config.allowed_users

    def install_routes(self):
        # Keep upstream's descriptions, parameters, defaults, permission metadata,
        # autocomplete, and choices. Only replace its callback with channel routing.
        for command in self.tree.get_commands():
            original, name = command.callback, command.name

            def routed_callback(callback, command_name):
                @functools.wraps(callback)
                async def routed(interaction, **kwargs):
                    return await self.dispatch(command_name, callback, interaction, kwargs)
                return routed

            command._callback = routed_callback(original, name)
            if name == 'fork':
                command._params['to'].default = 'same'
                command._params['to'].choices = [discord.app_commands.Choice(name=label, value=value)
                    for label, value in [('same backend', 'same'), ('Claude', 'claude'), ('Codex', 'codex')]]
                command._params['to'].description = 'Fork in the same backend, or hand the conversation to Claude/Codex'
            if name in {'model', 'globalmodel'}:
                command._params['name'].description = 'Model ID or alias; autocomplete follows this channel’s backend'
            if name == 'claude':
                command.description = 'Launch a Claude session in #claude'
            if name == 'astra':
                command.description = 'Launch a Codex session in #codex (compatibility alias)'
            if name not in {'claude', 'astra'}:
                command.description = command.description.replace("claude's", "session's").replace('claudes', 'sessions').replace('claude', 'session')[:100]
            # Autocomplete must use the selected backend, too.
            for param in command._params.values():
                if param.autocomplete is not None:
                    original_auto = param.autocomplete

                    def autocomplete_router(callback, command_name):
                        @functools.wraps(callback)
                        async def routed(interaction, current):
                            if self.backend_for(interaction.channel) == 'codex':
                                return await self.codex.autocomplete(command_name, current)
                            return await callback(interaction, current)
                        return routed

                    param.autocomplete = autocomplete_router(original_auto, name)

        @self.tree.command(name='codex', description='Launch a new Codex session in #codex')
        async def codex(interaction: discord.Interaction, prompt: str, project: str = ''):
            await self.dispatch('codex', None, interaction, {'prompt': prompt, 'project': project})

        @self.tree.command(name='stop', description='Interrupt the active turn without ending the conversation')
        async def stop(interaction: discord.Interaction):
            if self.backend_for(interaction.channel) == 'codex':
                return await self.dispatch('stop', None, interaction, {})
            return await self.original_commands['key'](interaction, key='esc')

        @self.tree.error
        async def error(interaction, exc):
            LOG.error('Chert command failed: %s', exc, exc_info=exc)
            text = str(getattr(exc, 'original', exc))[:1800]
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)

    async def dispatch(self, name, original, interaction, kwargs):
        if not self.allowed_user(interaction.user):
            return await interaction.response.send_message('This Chert instance is restricted.', ephemeral=True)
        backend = self.backend_for(interaction.channel)
        if name in {'codex', 'astra'}:
            backend = 'codex'
        elif name == 'claude':
            backend = 'claude'
        if backend is None:
            return await interaction.response.send_message('Use this command in #codex or #claude.', ephemeral=True)
        if name in {'model', 'globalmodel', 'fast'} and not self.privileged(interaction.user):
            return await interaction.response.send_message(f'/{name} is owner-only.', ephemeral=True)
        if backend == 'claude':
            return await self.claude.execute(name, original, interaction, kwargs)
        if not self.codex.allowed(interaction.user.id, self.codex.main_channel):
            return await interaction.response.send_message('You are not on the configured allowlist.', ephemeral=True)
        await interaction.response.defer(thinking=True)
        async def respond(text):
            await interaction.followup.send(text, allowed_mentions=upstream.NO_PING, suppress_embeds=True)
        await self.codex.execute(name, interaction.channel, interaction.user, kwargs, respond)

    async def astra_start(self, parent, user, cwd, prompt, respond, **kwargs):
        # Upstream's cross-backend handoff construction remains the source of truth;
        # only its destination changes from the old Astra sidecar to #codex.
        async with self.codex.session_creation_lock:
            thread = await self.codex._start_session(prompt, cwd=Path(cwd))
        await respond(f'🚀 Codex → {thread.mention}')
        return thread

    async def deliver_session_file(self, path, caption, pane, sid, cwd, backend=None):
        matches = [s for s in self.codex.store.sessions.values()
                   if (sid and s.codex_thread == sid) or (not sid and cwd and s.cwd == cwd and s.status != 'ended')]
        if len(matches) == 1:
            session = matches[0]
            target = self.codex.main_channel if session.status == 'ended' else await self.codex.live_channel(session)
            if Path(path).stat().st_size > upstream.FILE_LIMIT_BYTES:
                return await self.codex.say(target, f'File exceeds Discord limit: `{path}`')
            return await target.send(
                caption or None, file=discord.File(path), allowed_mentions=upstream.NO_PING)
        if len(matches) > 1:
            raise ValueError('More than one Codex session uses that directory; provide its session ID.')
        if backend == 'codex':
            if Path(path).stat().st_size > upstream.FILE_LIMIT_BYTES:
                raise ValueError('File exceeds the Discord upload limit.')
            return await self.codex.main_channel.send(caption or None, file=discord.File(path), allowed_mentions=upstream.NO_PING)
        return await super().deliver_session_file(path, caption, pane, sid, cwd)

    async def close(self):
        await self.codex.shutdown()
        titles = list(self._title_tasks)
        for session in self.codex.store.sessions.values():
            if session.discord_thread in self._retitling:
                session.thread_title_cache = ''
        if titles:
            self.codex.store.save()
        for task in titles:
            task.cancel()
        await asyncio.gather(*titles, return_exceptions=True)
        poller = getattr(self, 'poller', None)
        if poller:
            poller.cancel()
            await asyncio.gather(poller, return_exceptions=True)
        runner = getattr(self, '_hook_runner', None)
        if runner:
            await runner.cleanup()
        await super().close()


class ChannelInteraction:
    """Keep Discord response handles, but select the backend's destination channel."""
    def __init__(self, interaction, channel):
        self.original, self.channel, self.channel_id = interaction, channel, channel.id

    def __getattr__(self, name):
        return getattr(self.original, name)


def main():
    config = Config.from_env()
    config.channel_id = int(os.environ.get('DISCORD_CODEX_CHANNEL_ID') or config.channel_id)
    claude_id = int(os.environ.get('DISCORD_CLAUDE_CHANNEL_ID') or 0)
    if claude_id == config.channel_id or (not claude_id and os.environ.get('CHERT_BACKEND', 'both') == 'both'):
        raise SystemExit('Run setup_discord.py --backend both to configure distinct #codex and #claude channels.')
    import shutil
    binary = os.environ.get('CODEX_BIN') or shutil.which('codex') or str(Path.home() / '.local/bin/codex')
    runner = CodexRunner(binary, os.environ.get('CODEX_SANDBOX') or 'workspace-write',
                         float(os.environ.get('CODEX_TURN_TIMEOUT') or 10800),
                         Path(os.environ.get('CODEX_LOG_DIR') or 'private/codex-logs'),
                         os.environ.get('CODEX_NETWORK_ACCESS', '0') == '1')
    # State remains compatible with both original installations.
    upstream.load_state()
    if upstream.ash_twin is not None:
        home = Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')
        upstream.ash_twin.PROTECTED.add(home)
        upstream.ash_twin.BACKUP_SETS.extend([
            (home/'sessions', True, '*.jsonl'), (home/'archived_sessions', True, '*.jsonl'),
            (home/'config.toml', False, None), (home/'AGENTS.md', False, None), (config.state_file.resolve(), False, None),
            (config.state_file.resolve().parent/'codex-transcripts', True, '*.jsonl')])
    logging.basicConfig(level=logging.INFO)
    import fcntl
    config.state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(str(config.state_file) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        SharedFrontend(config, runner, SessionStore(config.state_file), claude_id).run(config.token)
