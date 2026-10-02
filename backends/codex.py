"""Codex adapter for upstream Chert's command surface and Discord gateway."""
import asyncio
import json
import logging
from pathlib import Path
import time
import uuid
from datetime import datetime, timezone
import os
import re

import discord

import discord_bot as upstream
from backends import codex_storage
from backends.codex_terminal import CodexTerminal
from codex_backend import Session, project_path
from codex_bot import CodexBot
from codex_live import RpcError
from codex_presentation import prompt_name

LOG = logging.getLogger(__name__)


class CodexChannel(CodexBot):
    def __init__(self, config, runner, store, frontend):
        self.frontend = frontend
        super().__init__(config, runner, store, register_commands=False)
        self.broadcast_channel = self.chat_channel = None
        self.chat_log = Path(os.environ.get('CODEX_CHAT_LOG') or Path.home()/'shared/codex_chat/msgs.jsonl')
        self.round_lock = asyncio.Lock()
        self.hub_sinks = set()
        self.terminal = CodexTerminal(runner.binary, self.live.socket)
        self.event_locks = {}

    def bind_gateway(self):
        self._connection = self.frontend._connection
        self.http, self.loop = self.frontend.http, self.frontend.loop

    async def wait_until_ready(self):
        await self.frontend.wait_until_ready()

    def is_closed(self):
        return self.frontend.is_closed()

    async def start_backend(self):
        self.main_channel = await self.fetch_channel(self.config.channel_id)
        guild = await self.fetch_guild(self.main_channel.guild.id)
        self.owner = self.config.owner_id or guild.owner_id
        broadcast = int(os.environ.get('DISCORD_CODEX_BROADCAST_CHANNEL_ID') or 0)
        chat = int(os.environ.get('DISCORD_CODEX_CHAT_CHANNEL_ID') or 0)
        if broadcast:
            self.broadcast_channel = await self.fetch_channel(broadcast)
            if self.store.meta.get('hub'):
                self.hub_sinks.add(broadcast)
        if chat:
            self.chat_channel = await self.fetch_channel(chat)
        await self.webhook_for()
        self.background_tasks = [asyncio.create_task(self.live_events()), asyncio.create_task(self.maintenance())]
        if self.config.discover:
            self.background_tasks.append(asyncio.create_task(self.discover_loop()))

    def allowed(self, user_id, channel):
        if channel and channel.id in self.store.sessions:
            return user_id == self.owner or user_id in self.config.allowed_users
        if channel and channel.id in {getattr(self.broadcast_channel, 'id', None), getattr(self.chat_channel, 'id', None)}:
            return user_id == self.owner or user_id in self.config.allowed_users
        return super().allowed(user_id, channel)

    async def shutdown(self):
        tasks = self.background_tasks + list(self.workers.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.live.close()

    async def webhook_for(self):
        return await self.frontend.webhook_for(self.main_channel)

    async def say(self, channel, text):
        session = self.store.sessions.get(getattr(channel, 'id', None))
        if session is None:
            return await self.frontend.say(channel, str(text))
        name = upstream.webhook_name({'project': Path(session.cwd).name, 'name': session.name})
        entries = upstream.format_new_items([{'kind': 'assistant', 'text': str(text)}], session.codex_thread or '', include_tools=False)
        message = None
        for _, content in entries:
            if content.startswith('-# …truncated — '):
                content = content.replace(upstream.DASHBOARD, upstream.DASHBOARD.removesuffix('/claudes') + '/codex')
            message = await self.frontend.post_as(self.main_channel, name, content,
                                                   session.discord_thread, seed=str(session.discord_thread))
        return message

    async def _start_session(self, prompt, project='', codex_id=None, source_message=None, cwd=None):
        if source_message and source_message.id in self.store.sessions:
            return await self.fetch_channel(source_message.id)
        old = next((s for s in self.store.sessions.values() if s.codex_thread == codex_id), None) if codex_id else None
        if old:
            await self.ensure_live(old)
            thread = await self.live_channel(old)
            old.status = 'idle' if old.status == 'ended' else old.status
            self.store.save()
            return thread
        await self.live.connect()
        if codex_id:
            codex_id = str(uuid.UUID(codex_id))
            try:
                result = await self.live.call('thread/resume', {'threadId': codex_id, 'excludeTurns': True})
            except RpcError:
                if not await asyncio.to_thread(codex_storage.restore, codex_id):
                    raise
                result = await self.live.call('thread/resume', {'threadId': codex_id, 'excludeTurns': True})
        else:
            if cwd is None:
                cwd, remaining = upstream.resolve_project([project] if project else [])
                if remaining:
                    raise ValueError(f'Project directory does not exist: {project}')
            cwd = Path(cwd).expanduser().resolve()
            params = {'cwd': str(cwd), 'approvalPolicy': 'on-request', 'sandbox': self.runner.sandbox,
                      'config': {'sandbox_workspace_write.network_access': self.runner.network}}
            if upstream.PERMISSION_MODE == 'auto':
                params['approvalsReviewer'] = 'auto_review'
            if self.store.meta.get('yolo_until', 0) > time.time():
                params.update(approvalPolicy='never', sandbox='danger-full-access')
            model = self.store.meta.get('model', self.config.model)
            if model:
                params['model'] = model
            if self.config.effort:
                params['config']['model_reasoning_effort'] = self.config.effort
            result = await self.live.call('thread/start', params)
        info = result['thread']
        title = prompt_name(prompt) if prompt else info.get('name') or f'codex-{info["id"][:8]}'
        if source_message:
            await source_message.add_reaction('🚀')
            thread = await source_message.create_thread(name=upstream.thread_title(title, info['id'], False),
                                                        auto_archive_duration=10080)
        else:
            thread = await self.main_channel.create_thread(name=upstream.thread_title(title, info['id'], False),
                                                           type=discord.ChannelType.public_thread, auto_archive_duration=10080)
        session = Session(thread.id, info['cwd'], title, info['id'], backend='app-server',
                          native_settings=True,
                          mirror_since=time.time(),
                          display_model=result.get('model') or info.get('model') or '',
                          source_message=source_message.id if source_message else None)
        session.service_tier = self.store.meta.get('service_tier')
        self.store.sessions[thread.id] = session
        self.live.subscribed.add(info['id'])
        self.store.save()
        self.log_event(f'🚀 {title} arrived')
        await self.live.call('thread/name/set', {'threadId': info['id'], 'name': title})
        card = await self.say(thread, f'**{title}** · `{Path(session.cwd).name}`\nReply to talk · `!help`')
        session.status_message, session.status_webhook = card.id, True
        self.store.save()
        if prompt:
            await self.send_prompt(thread, prompt)
            if source_message:
                await source_message.add_reaction('📡')
            else:
                await self.say(thread, f'-# 🧑 {prompt}')
        return thread

    async def launch_message(self, message, prompt):
        cwd, words = upstream.resolve_project(prompt.split())
        prompt = ' '.join(words).strip()
        if not prompt:
            return await self.say(message.channel, 'Include a prompt after the project directory.')
        source = message if message.channel.id == self.config.channel_id else None
        async with self.session_creation_lock:
            thread = await self._start_session(prompt, source_message=source, cwd=cwd)
        if source is None:
            await self.say(message.channel, f'Continue in {thread.mention}')

    async def send_prompt(self, thread, prompt):
        session = self.store.sessions[thread.id]
        if session.status == 'ended':
            raise ValueError('This session ended. Use /revive or /resume first.')
        await self.ensure_live(session)
        await self.apply_pending_settings(session)
        # Old exec-created conversations are resumed through the same daemon too.
        if session.backend == 'exec' and session.codex_thread and thread.id not in self.workers:
            await self.live.connect()
            await self.live.attach(session.codex_thread)
            session.backend = 'app-server'
        result = await super().send_prompt(thread, prompt)
        if result == '👀':
            # Native turn settings persist in the daemon. Don't overwrite a later
            # change made from the user's terminal/editor on every Discord reply.
            session.service_tier = None
            session.collaboration_mode = None
            self.store.save()
        return result

    def record(self, session, kind, text, key=None, tool_id=None, error=False):
        if key and key in session.journal_seen:
            return
        if key:
            session.journal_seen = (session.journal_seen + [key])[-1000:]
        session.recent = (session.recent + [{'kind': kind, 'text': str(text)[:8000], 'time': time.time()}])[-200:]
        folder = self.store.path.parent / 'codex-transcripts'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        timestamp = datetime.now(timezone.utc).isoformat()
        if kind == 'user':
            row = {'type': 'user', 'origin': {'kind': 'human'}, 'timestamp': timestamp,
                   'message': {'content': str(text)}}
        elif kind in {'agentMessage', 'thinking'}:
            row = {'type': 'assistant', 'timestamp': timestamp,
                   'message': {'content': ([{'type': 'text', 'text': str(text)}] if kind == 'agentMessage'
                                           else [{'type': 'thinking', 'thinking': str(text)}])}}
        elif kind == 'result':
            row = {'type': 'user', 'timestamp': timestamp,
                   'message': {'content': [{'type': 'tool_result', 'tool_use_id': tool_id,
                                            'content': str(text), 'is_error': error}]}}
        else:
            tool_name = {'commandExecution': 'Bash', 'fileChange': 'Edit', 'mcpToolCall': 'MCP',
                         'webSearch': 'WebSearch', 'collabAgentToolCall': 'Agent'}.get(kind, kind)
            row = {'type': 'assistant', 'timestamp': timestamp,
                   'message': {'content': [{'type': 'tool_use', 'id': key or '', 'name': tool_name,
                                            'input': {'command': str(text)}}]}}
        path = folder / f'{session.codex_thread}.jsonl'
        with os.fdopen(os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600), 'a') as file:
            file.write(json.dumps(row) + '\n')
        self.store.save()

    async def handle_live_event(self, event):
        sid = (event.get('params') or {}).get('threadId', '')
        async with self.event_locks.setdefault(sid, asyncio.Lock()):
            try:
                await self._handle_live_event(event)
            except Exception:
                session = next((s for s in self.store.sessions.values() if s.codex_thread == sid), None)
                if session:
                    session.delivery_failed = True
                    self.store.save()
                raise

    async def _handle_live_event(self, event):
        params = event.get('params') or {}
        session = next((s for s in self.store.sessions.values() if s.codex_thread == params.get('threadId')), None)
        if session:
            if event['method'] == 'thread/name/updated':
                name = params.get('threadName') or params.get('name')
                if name:
                    await self.observe_session(session, {'name': name})
                return
            if event['method'] == 'chert/inputRequired':
                return await self.show_request(session, event)
            if event['method'] == 'turn/started':
                session.activity = {'started': time.time(), 'counts': {}}
                if not session.muted:
                    from codex_presentation import activity_text
                    card = await self.say(await self.live_channel(session), activity_text(session, 'running', time.time()))
                    session.status_message, session.status_webhook = card.id, True
            if event['method'] == 'turn/completed':
                session.last_completed_at = time.time()
                self.store.save()
            if event['method'] in {'item/started', 'item/completed'}:
                item = params.get('item') or {}
                kind = item.get('type')
                if kind in {'commandExecution', 'fileChange', 'mcpToolCall', 'webSearch', 'collabAgentToolCall'}:
                    session.activity.setdefault('counts', {})
                    session.activity.setdefault('started', time.time())
                    seen = session.activity.setdefault('seen_tools', [])
                    if item.get('id') not in seen:
                        seen.append(item.get('id'))
                        state = {'card': session.activity}
                        names = {'commandExecution': 'Bash', 'fileChange': 'Edit', 'mcpToolCall': 'MCP',
                                 'webSearch': 'WebSearch', 'collabAgentToolCall': 'Agent'}
                        upstream.update_card(state, [{'kind': 'tool', 'name': names[kind],
                            'text': item.get('command') or item.get('query') or item.get('tool', ''),
                            'desc': item.get('description', '')}])
                        session.activity = state['card']
                        self.store.save()
                        if not session.muted:
                            await self.update_live_status(session)
            if event['method'] == 'item/completed':
                item = params.get('item') or {}
                key = f'{params.get("turnId", "")}:{item.get("id", "")}'
                if item.get('type') == 'reasoning' and item.get('summary'):
                    summary = item['summary']
                    text = summary if isinstance(summary, str) else '\n'.join(
                        x.get('text', '') if isinstance(x, dict) else str(x) for x in summary)
                    session.activity.setdefault('counts', {})
                    state = {'card': session.activity}
                    upstream.update_card(state, [{'kind': 'thinking', 'text': text}])
                    self.record(session, 'thinking', text, key)
                    if not session.muted:
                        await self.update_live_status(session)
                if item.get('type') == 'userMessage':
                    self.record(session, 'user', '\n'.join(i.get('text', '') for i in item.get('content', [])), key)
                if item.get('type') == 'imageView' and item.get('path') and not session.muted:
                    await self.frontend.deliver_session_file(item['path'], '', None, session.codex_thread, session.cwd)
                if item.get('type') in {'agentMessage', 'commandExecution', 'fileChange', 'mcpToolCall', 'webSearch', 'collabAgentToolCall'}:
                    self.record(session, item['type'], item.get('text') or item.get('command') or item.get('query') or item.get('tool') or item.get('type'), key)
                if item.get('type') == 'commandExecution' and item.get('aggregatedOutput'):
                    self.record(session, 'result', item['aggregatedOutput'], key + ':result',
                                tool_id=key, error=item.get('exitCode') not in (None, 0))
                if item.get('type') == 'agentMessage' and session.discord_thread == self.store.meta.get('hub'):
                    for channel_id in list(self.hub_sinks):
                        target = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
                        await self.frontend.say(target, item.get('text', ''))
                if session.muted:
                    return
        await super().handle_live_event(event)
        if session and event['method'] == 'turn/completed' and not session.delivery_failed:
            turn = params['turn']
            session.mirrored_turns = (session.mirrored_turns + [turn['id']])[-1000:]
            session.mirror_since = max(session.mirror_since, turn.get('completedAt') or time.time())
            self.store.save()

    async def update_live_status(self, session):
        if session.muted:
            return
        now = time.time()
        if session.status == 'running' and now - session.activity.get('last_edit', 0) < upstream.CARD_MIN_GAP:
            return
        result = await super().update_live_status(session)
        session.activity['last_edit'] = now
        return result

    async def save_attachments(self, message):
        # Use the exact upstream upload naming, limits, and local-path convention.
        return await self.frontend.save_attachments(message)

    async def on_message(self, message):
        if message.author.id == self.frontend.user.id or message.webhook_id:
            return
        if not self.allowed(message.author.id, message.channel):
            return
        content = (message.content or '').strip()
        if self.chat_channel and message.channel.id == self.chat_channel.id:
            self.chat_log.parent.mkdir(parents=True, exist_ok=True)
            with self.chat_log.open('a') as file:
                file.write(json.dumps({'ts': time.strftime('%H:%M:%S'), 'from': message.author.display_name,
                                       'text': content, 'via': 'discord'}) + '\n')
            return await message.add_reaction('📨')
        if self.broadcast_channel and message.channel.id == self.broadcast_channel.id and not content.startswith('!'):
            attached = await self.save_attachments(message) if message.attachments else ''
            task = asyncio.create_task(self.ask_round(message, f'{content}\n{attached}'.strip()))
            self.background_tasks.append(task)
            task.add_done_callback(lambda done: self.background_tasks.remove(done) if done in self.background_tasks else None)
            return
        if content.startswith('!'):
            command, _, value = content[1:].partition(' ')
            if command.lower() in {'codex', 'astra'} and message.channel.id == self.config.channel_id:
                if not value.strip():
                    return await self.say(message.channel, 'Include a prompt after the command.')
                return await self.launch_message(message, value)
            args = text_arguments(command.lower(), value)
            return await self.execute(args.pop('_command', command.lower()), message.channel, message.author,
                                      args, lambda text: self.say(message.channel, text))
        attached = await self.save_attachments(message) if message.attachments else ''
        if attached:
            from types import SimpleNamespace
            message = MessageWithAttachments(message, f'{content}\n{attached}'.strip())
        return await super().on_message(message)

    async def ensure_live(self, session):
        await self.live.connect()
        if session.discord_thread in self.workers:
            raise ValueError('This older exec turn is still running. Stop it or wait before using this control.')
        if not session.codex_thread:
            async with self.session_creation_lock:
                if not session.codex_thread:
                    result = await self.live.call('thread/start', {'cwd': session.cwd,
                        'approvalPolicy': 'on-request', 'sandbox': self.runner.sandbox})
                    session.codex_thread = result['thread']['id']
                    self.live.subscribed.add(session.codex_thread)
                    session.backend = 'app-server'
                    self.store.save()
        try:
            await self.live.attach(session.codex_thread)
        except RpcError:
            restored = await asyncio.to_thread(codex_storage.restore, session.codex_thread)
            if not restored:
                await self.live.call('thread/unarchive', {'threadId': session.codex_thread})
            await self.live.attach(session.codex_thread)
        if session.backend == 'exec':
            session.mirror_since = time.time()  # Its earlier exec replies were already posted.
        session.backend = 'app-server'
        session.native_settings = True
        self.store.save()

    async def apply_pending_settings(self, session):
        if not session.pending_settings or await self.live.active_turn(session.codex_thread):
            return
        response = await self.live.call('thread/resume', {
            'threadId': session.codex_thread, 'excludeTurns': True, **session.pending_settings})
        session.display_model = response.get('model') or session.display_model
        session.pending_settings.clear()
        self.store.save()

    async def stop(self, thread, end=False):
        session = self.store.sessions[thread.id]
        if session.backend == 'app-server':
            await self.live.interrupt(session.codex_thread)
            deadline = time.monotonic() + 15
            while await self.live.active_turn(session.codex_thread):
                if time.monotonic() >= deadline:
                    raise ValueError('Codex has not finished interrupting this turn yet. Try again shortly.')
                await asyncio.sleep(0.2)
        await super().stop(thread, end=end)
        if end and session.backend == 'app-server':
            await self.terminal.close(session)
            await self.live.call('thread/archive', {'threadId': session.codex_thread})
            self.live.subscribed.discard(session.codex_thread)
            session.deadline = None
            self.store.save()

    async def execute(self, name, channel, user, args, respond):
        if name in {'model', 'globalmodel', 'fast'} and user.id != self.owner:
            return await respond(f'/{name} is owner-only.')
        if name in {'codex', 'astra'}:
            thread = await self.start_session(args.get('prompt', ''), args.get('project', ''))
            return await respond(f'🚀 launched → {thread.mention}')
        if name == 'help':
            lines = ['**Chert · Codex** — same command surface as #claude.']
            for command in self.frontend.tree.get_commands():
                if command.name in {'claude', 'astra'}:
                    continue
                lines.append(f'`/{command.name}` — {command.description}')
            lines.append('`/screen` opens a terminal client for this same Codex conversation; `/key` controls it.')
            for part in upstream.split_chunks('\n'.join(lines)):
                await respond(part)
            return
        if name == 'sessions':
            return await respond(self.session_list())
        if name == 'resume':
            query = args.get('session', '')
            rows = await self.search_sessions(query)
            if len(rows) != 1:
                await channel.send(upstream.resume_text(query, 0, rows), view=upstream.resume_view(query, 0, rows),
                                   allowed_mentions=upstream.NO_PING)
                return await respond('Choose a session from the list.')
            thread = await self.start_session('', codex_id=rows[0]['id'])
            return await respond(f'♻️ resumed → {thread.mention}')
        if name in {'disk', 'backup', 's3', 'offload', 'restore'}:
            text = '!' + name
            if args.get('directory'):
                text += ' ' + args['directory']
            if args.get('confirm'):
                text += ' confirm'
            proxy = SimpleMessage(channel, user, text)
            await self.frontend.handle_main_command(proxy, text)
            return await respond('Done.')
        if name == 'all':
            count = 0
            for s in list(self.store.sessions.values()):
                if s.status != 'ended' and s.discord_thread != self.store.meta.get('hub'):
                    await self.send_prompt(await self.live_channel(s), args['message'])
                    count += 1
            return await respond(f'📣 Sent to {count} sessions.')
        if name == 'globalmodel':
            model = await self.resolve_model(args['name'])
            self.store.meta['model'] = model
            self.config.model = model
            for s in self.store.sessions.values():
                s.model = model
                if s.status != 'ended':
                    s.pending_settings['model'] = model
            self.store.save()
            from setup_discord import ENV, write_env
            if ENV.exists():
                write_env({'CODEX_MODEL': model})
            return await respond(f'🧠 Model for this backend and new sessions: `{model}`')
        if name == 'fast':
            current = self.store.sessions.get(channel.id)
            targets = list(self.store.sessions.values()) if args.get('everywhere') or current is None else [current]
            tier = 'fast' if args.get('mode', 'on') == 'on' else 'default'
            for target in targets:
                target.service_tier = tier
            if args.get('everywhere') or current is None:
                self.store.meta['service_tier'] = tier
            self.store.save()
            return await respond('Fast setting saved for subsequent turns; account/model availability is enforced by Codex.')
        if name == 'feldspar':
            if not self.frontend.claude_enabled:
                return await respond('Feldspar uses both Claude and Codex reviewers. Sign in to Claude to enable it.')
            current = self.store.sessions.get(channel.id)
            focus = args.get('focus', '')
            if current:
                transcript = self.store.path.parent/'codex-transcripts'/f'{current.codex_thread}.jsonl'
                target = {'cwd': current.cwd, 'name': current.name, 'sid': current.codex_thread,
                          'transcript': str(transcript) if transcript.exists() else None}
            else:
                cwd, words = upstream.resolve_project(focus.split())
                target = {'cwd': str(cwd), 'name': cwd.name, 'sid': None, 'transcript': None}
                focus = ' '.join(words)
            return await self.frontend.feldspar(self.frontend.main_channel, user, target, focus, respond)
        if name in {'restartall', 'reviveall', 'cleanup'}:
            count = 0
            for s in list(self.store.sessions.values()):
                target = await self.live_channel(s)
                if name == 'cleanup' and s.status == 'ended':
                    if args.get('delete'):
                        await target.delete()
                        self.store.sessions.pop(s.discord_thread)
                    else:
                        await target.edit(archived=True)
                    count += 1
                elif name == 'reviveall' and s.status in {'ended', 'interrupted', 'disconnected', 'error'}:
                    await self.revive(s, target, False)
                    count += 1
                elif name == 'restartall' and s.status != 'ended' and (s.status != 'running' or args.get('force')):
                    await self.restart(s, target, bool(args.get('force')))
                    count += 1
            self.store.save()
            return await respond(f'{name}: {count} sessions.')
        if name == 'yolo':
            duration = args['duration']
            seconds = 0 if duration == 'off' else upstream.parse_duration(duration)
            if seconds is None or seconds > upstream.YOLO_MAX:
                raise ValueError('Use a duration such as 30m or 1h (at most 12h), or off.')
            self.store.meta['yolo_until'] = time.time() + seconds if seconds else 0
            self.store.save()
            return await respond('Bypass for new Codex sessions enabled until the countdown expires.' if seconds else 'Bypass disabled for new sessions.')
        if name == 'hub':
            return await self.hub(args['message'], respond, channel)
        session = self.store.sessions.get(channel.id)
        if session is None:
            raise ValueError('Use this command in a session thread.')
        if session.status == 'ended' and name not in {'revive', 'fork', 'log', 'mute', 'unmute', 'kill'}:
            return await respond('🌌 This session ended. Use /revive or /resume first.')
        if name in {'screen', 'key'}:
            await self.ensure_live(session)
            if name == 'key':
                key = upstream.KEYMAP.get(args.get('key', '').lower())
                if not key:
                    raise ValueError('Choose a key from /key autocomplete or !help.')
                await self.terminal.key(session, key)
                self.store.save()
                return await respond('✅ Key sent.')
            text = await self.terminal.screen(session)
            self.store.save()
            options = upstream.parse_options(text)[0] if session.status == 'waiting' else []
            await channel.send(upstream.prompt_body(text),
                view=upstream.prompt_view('codex:' + str(session.discord_thread), options),
                allowed_mentions=upstream.NO_PING)
            return await respond('🖥 Terminal view updated.')
        if name in {'mute', 'unmute'}:
            session.muted = name == 'mute'
            self.store.save()
            return await respond('🔇 muted' if session.muted else '🔊 unmuted')
        if name in {'kill', 'stop'}:
            await self.stop(channel, end=name == 'kill')
            if name == 'kill' and args.get('how') == 'delete':
                await channel.delete()
            return await respond('Done.')
        if name in {'restart', 'revive', 'refresh'}:
            if name == 'revive' and args.get('mode') == 'fork':
                return await self.fork(session, '', respond)
            force = args.get('force') or args.get('mode') == 'force'
            if name == 'restart' and session.status == 'running' and not force:
                return await respond('This session is busy; use force to interrupt it.')
            if name == 'restart' or (name == 'refresh' and force):
                await self.restart(session, channel, force)
            else:
                await self.revive(session, channel, force or name == 'refresh')
            if args.get('message'):
                await self.send_prompt(channel, args['message'])
            return await respond('♻️ session ready; history and Discord thread retained.')
        if name == 'fork':
            if args.get('to') == 'claude':
                return await self.handoff_to_claude(session, args.get('message', ''), user, respond)
            return await self.fork(session, args.get('message', ''), respond)
        if name == 'log':
            await self.live.connect()
            result = await self.live.call('thread/turns/list', {'threadId': session.codex_thread,
                'limit': min(int(args.get('count', 25)), 200), 'itemsView': 'summary', 'sortDirection': 'desc'})
            entries = []
            for turn in reversed(result['data']):
                for item in turn.get('items', []):
                    body = item.get('text') or item.get('command') or item.get('query') or item.get('tool') or item.get('type', '')
                    if item.get('type') == 'userMessage':
                        body = ' '.join(x.get('text', '') for x in item.get('content', []))
                    kind = {'agentMessage': 'assistant', 'userMessage': 'user'}.get(item.get('type'), 'tool')
                    entries.append(('', kind, str(body)))
            text = upstream.render_ship_log(session.name, entries[-min(int(args.get('count', 25)), 200):])
            for chunk in upstream.split_chunks(text)[:4]:
                await respond(chunk)
            return
        if name == 'supernova':
            old = session.deadline
            if args.get('cancel'):
                session.deadline = None
                if old and old.get('message'):
                    await channel.get_partial_message(old['message']).edit(content='☀️ Countdown cancelled.')
            else:
                seconds = args.get('seconds') or int(args.get('minutes', 22)) * 60
                session.deadline = {'until': time.time() + seconds, 'action': args.get('then', 'wrap'), 'warned': []}
                card = await channel.send(f'☀️ Supernova <t:{int(session.deadline["until"])}:R>', allowed_mentions=upstream.NO_PING)
                session.deadline['message'] = card.id
            self.store.save()
            return await respond('Countdown cancelled.' if not session.deadline else f'☀️ Supernova <t:{int(session.deadline["until"])}:R>')
        if name in {'model', 'effort', 'rename'}:
            await self.ensure_live(session)
            value = args.get('name') or args.get('level') or args.get('value', '')
            if name == 'model':
                value = await self.resolve_model(value)
                session.pending_settings['model'] = value
            if name == 'effort':
                from codex_bot import EFFORTS
                if value not in (*EFFORTS, 'default'):
                    raise ValueError('Choose a supported reasoning effort.')
                if value == 'default':
                    cfg = await self.live.call('config/read', {})
                    effort = cfg.get('config', {}).get('model_reasoning_effort')
                    if not effort:
                        raise ValueError('No default effort is configured; choose an explicit effort.')
                    value = effort
                session.pending_settings.setdefault('config', {})['model_reasoning_effort'] = value
            if name == 'rename':
                if not value.strip():
                    raise ValueError('Provide a session name.')
                await self.live.call('thread/name/set', {'threadId': session.codex_thread, 'name': value})
                session.name = value.strip()[:90]
                await self.observe_session(session, {'name': session.name})
            else:
                setattr(session, name, value)
            self.store.save()
            await self.apply_pending_settings(session)
            return await respond(f'✅ {name} → `{value}`' + (' · queued until idle' if session.pending_settings else ''))
        if name == 'mode':
            await self.set_mode(session, args['mode'])
            return await respond(f'Permission mode: {args["mode"]}')
        raise ValueError(f'No Codex mapping for /{name}; this is a parity defect, not a successful command.')

    async def resolve_model(self, value):
        if value == 'default':
            if self.config.model:
                return self.config.model
            await self.live.connect()
            result = await self.live.call('config/read', {})
            value = result.get('config', {}).get('model') or ''
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}', value):
            raise ValueError('Choose a model from autocomplete or provide an explicit model ID.')
        return value

    async def revive(self, session, channel, force):
        if force:
            await self.stop(channel)
        await self.ensure_live(session)
        info = (await self.live.call('thread/read', {'threadId': session.codex_thread, 'includeTurns': False}))['thread']
        from codex_live import live_status
        session.status = live_status(info)
        await channel.edit(archived=False)
        self.store.save()

    async def restart(self, session, channel, force):
        if session.status == 'running' and not force:
            raise ValueError('This session is busy; use force to interrupt it.')
        await self.live.connect()
        if force:
            await self.live.interrupt(session.codex_thread)
        # Archive/unarchive unloads only this thread's actor, not the shared daemon.
        # Always restore the persisted history before attempting to reload it.
        await self.live.call('thread/archive', {'threadId': session.codex_thread})
        self.live.subscribed.discard(session.codex_thread)
        await self.live.call('thread/unarchive', {'threadId': session.codex_thread})
        await self.revive(session, channel, False)

    async def handoff_to_claude(self, session, message, user, respond):
        if not self.frontend.claude_enabled:
            return await respond('Claude must be signed in before receiving a handoff.')
        await self.ensure_live(session)
        history = await self.live.call('thread/turns/list', {'threadId': session.codex_thread,
            'limit': 100, 'itemsView': 'summary', 'sortDirection': 'desc'})
        folder = Path(session.cwd)
        handoff = upstream.REPORTS_DIR / f'codex-handoff-{session.codex_thread}.md'
        handoff.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for turn in reversed(history['data']):
            for item in turn.get('items', []):
                if item.get('type') == 'userMessage':
                    lines.append('**USER:** ' + '\n'.join(p.get('text', '') for p in item.get('content', [])))
                elif item.get('type') == 'agentMessage':
                    lines.append('**CODEX:** ' + item.get('text', ''))
                else:
                    lines.append(f'[tool {item.get("type")}: {str(item.get("command") or item.get("tool") or "")[:220]}]')
        handoff.write_text(f'# Handoff from {session.name}\n\n' + '\n\n'.join(lines)[-upstream.ASTRA_HANDOFF_CHARS:])
        prompt = f'Continue the conversation saved in {handoff}. ' + (message or 'Read it and wait for my next instruction.')
        proxy = self.frontend.main_channel
        pane, error = await asyncio.to_thread(upstream.spawn_claude, session.name + '-fork', str(folder))
        if not pane:
            raise ValueError(error)
        running, notes = await self.frontend.await_registration(pane)
        if not running:
            raise ValueError('Claude did not register; check its login and terminal.')
        thread = await self.frontend.adopt_session(running, proxy)
        await asyncio.to_thread(self.frontend.deliver, running, user, prompt)
        await respond(f'🌱 handoff → {thread.mention}')

    async def fork(self, session, message, respond):
        await self.live.connect()
        result = await self.live.call('thread/fork', {'threadId': session.codex_thread, 'excludeTurns': True})
        await self.live.call('thread/name/set', {'threadId': result['thread']['id'], 'name': upstream.fork_name(session.name)})
        thread = await self.start_session('', codex_id=result['thread']['id'])
        if message:
            await self.send_prompt(thread, message)
        await respond(f'🌱 forked → {thread.mention}')

    async def search_sessions(self, query):
        await self.live.connect()
        try:
            uuid.UUID(query)
        except ValueError:
            cache = getattr(self, '_index_cache', None)
            if cache is None or time.time() - cache[0] > 15:
                rows = []
                for archived in (False, True):
                    cursor = None
                    while True:
                        result = await self.live.call('thread/list', {'limit': 100, 'archived': archived,
                            'cursor': cursor, 'sourceKinds': ['cli', 'vscode', 'exec', 'appServer', 'unknown']})
                        rows.extend(index_row(t) for t in result['data'])
                        cursor = result.get('nextCursor')
                        if not cursor:
                            break
                rows.sort(key=lambda r: -r['mtime'])
                existing = {r['sid'] for r in rows}
                backed_up = await asyncio.to_thread(codex_storage.backup_index)
                rows.extend(r for sid, r in backed_up.items() if sid not in existing)
                rows.sort(key=lambda r: -r['mtime'])
                self._index_cache = (time.time(), rows)
            return upstream.search_sessions(query, self._index_cache[1])
        try:
            result = await self.live.call('thread/read', {'threadId': query, 'includeTurns': False})
        except RpcError:
            rows = await asyncio.to_thread(codex_storage.backup_index)
            return [rows[query]] if query in rows else []
        return [index_row(result['thread'])]

    async def autocomplete(self, command, current):
        if command == 'resume':
            rows = await self.search_sessions(current)
            return [discord.app_commands.Choice(name=(r.get('name') or r.get('preview') or r['id'])[:100], value=r['id']) for r in rows[:25]]
        await self.live.connect()
        rows = (await self.live.call('model/list', {}))['data']
        return [discord.app_commands.Choice(name=r['model'][:100], value=r['model']) for r in rows
                if current.lower() in r['model'].lower()][:25]

    async def set_mode(self, session, mode):
        await self.ensure_live(session)
        params = {'threadId': session.codex_thread, 'excludeTurns': True}
        if mode == 'bypass':
            params.update(approvalPolicy='never', sandbox='danger-full-access')
        elif mode in {'auto', 'default'}:
            params.update(approvalPolicy='on-request', sandbox='workspace-write',
                          approvalsReviewer='auto_review' if mode == 'auto' else 'user')
        elif mode == 'plan':
            session.collaboration_mode = 'plan'
            session.permission_mode = mode
            self.store.save()
            return
        else:
            raise ValueError('Choose default, auto, bypass, or plan.')
        await self.live.call('thread/resume', params)
        session.collaboration_mode = 'default'
        session.permission_mode = mode
        self.store.save()

    async def hub(self, prompt, respond, target=None):
        thread_id = self.store.meta.get('hub')
        if thread_id in self.store.sessions:
            thread = await self.live_channel(self.store.sessions[thread_id])
        else:
            thread = await self.start_session('You are the persistent session coordinator. Wait for my next message.')
            self.store.meta['hub'] = thread.id
            self.store.save()
        await self.send_prompt(thread, prompt)
        target = self.broadcast_channel or target
        if target and target.id != thread.id:
            self.hub_sinks = {target.id}
        await respond(f'Hub: {thread.mention}')

    async def ask_round(self, message, prompt):
        async with self.round_lock:
            targets = [s for s in self.store.sessions.values() if s.status != 'ended' and s.discord_thread != self.store.meta.get('hub')]
            started = time.time()
            await message.add_reaction('📣')
            for session in targets:
                await self.send_prompt(await self.live_channel(session), prompt)
            deadline = time.monotonic() + upstream.ASK_COLLECT_SECS
            while targets and time.monotonic() < deadline and not all(s.last_completed_at >= started for s in targets):
                await asyncio.sleep(2)
            lines = ['Summarize these session replies to the user request:', prompt]
            for session in targets:
                replies = [r['text'] for r in session.recent if r['kind'] == 'agentMessage' and r['time'] >= started]
                lines += [f'\n## {session.name}', '\n'.join(replies)[-upstream.ASK_REPLY_MAX:] or '(no reply within the collection window)']
            await self.hub('\n'.join(lines), lambda text: self.say(message.channel, text), message.channel)

    async def maintenance(self):
        await self.wait_until_ready()
        boot = upstream.boot_id()
        previous = self.store.meta.get('boot_id')
        if upstream.REVIVE_ON_BOOT and previous and boot != previous:
            for session in list(self.store.sessions.values()):
                if session.status != 'ended':
                    try:
                        await self.ensure_live(session)
                    except Exception:
                        LOG.exception('Could not revive Codex session %s', session.codex_thread)
        self.store.meta['boot_id'] = boot
        self.store.save()
        while not self.is_closed():
            try:
                await self.board_tick()
                await self.chat_tick()
                await self.prompt_tick()
                await self.frontend.update_shared_presence()
                for session in list(self.store.sessions.values()):
                    if session.status == 'ended' and session.ended_at and time.time() - session.ended_at > upstream.ENDED_KEEP_DAYS * 86400:
                        self.store.sessions.pop(session.discord_thread, None)
                        self.store.save()
                        continue
                    if session.status != 'ended' and session.delivery_failed:
                        await self.catch_up(session)
                    if session.status == 'idle' and session.pending_settings:
                        await self.ensure_live(session)
                        await self.apply_pending_settings(session)
                    if session.status == 'running' and time.time() - session.activity.get('last_edit', 0) >= upstream.CARD_HEARTBEAT:
                        await self.update_live_status(session)
                    if upstream.REVIVE_ON_CRASH and session.status in {'disconnected', 'interrupted'}:
                        attempts = self.store.meta.setdefault('revive_attempts', {})
                        if time.time() - attempts.get(session.codex_thread, 0) > 60:
                            attempts[session.codex_thread] = time.time()
                            await self.ensure_live(session)
                    if session.status != 'ended' and session.deadline:
                        left = session.deadline['until'] - time.time()
                        if 0 < left <= 120 and 120 not in session.deadline.setdefault('warned', []):
                            session.deadline['warned'].append(120)
                            await self.frontend.say(await self.live_channel(session), '🔴 Two minutes to the supernova.', ping_owner=True)
                            self.store.save()
                    if session.status != 'ended' and session.deadline and session.deadline['until'] <= time.time():
                        action, message_id = session.deadline['action'], session.deadline.get('message')
                        session.deadline = None
                        self.store.save()
                        thread = await self.live_channel(session)
                        if message_id:
                            await thread.get_partial_message(message_id).edit(content='💥 SUPERNOVA — time is up.')
                        if action in {'stop', 'kill'}:
                            await self.stop(thread, end=action == 'kill')
                        if action != 'kill':
                            await self.send_prompt(thread, 'Supernova: the time budget is up. Wrap up, record what remains, and report here.')
            except Exception:
                LOG.exception('Codex maintenance failed')
            await asyncio.sleep(5)

    async def chat_tick(self):
        if not self.chat_channel or not self.chat_log.exists():
            return
        offset = self.store.meta.setdefault('chat_offset', self.chat_log.stat().st_size)
        if offset > self.chat_log.stat().st_size:
            offset = 0
        with self.chat_log.open('rb') as file:
            file.seek(offset)
            for _ in range(100):
                line = file.readline()
                if not line or not line.endswith(b'\n'):
                    break
                try:
                    row = json.loads(line)
                    if row.get('via') != 'discord' and row.get('text'):
                        await self.frontend.post_as(self.chat_channel, row.get('from') or 'Codex', row['text'])
                except ValueError:
                    pass
                self.store.meta['chat_offset'] = file.tell()
        self.store.save()

    async def prompt_tick(self):
        channel_id = int(os.environ.get('CODEX_PROMPT_CHANNEL_ID') or 0)
        if not channel_id:
            return
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        async for message in channel.history(limit=20):
            attachment = next((a for a in message.attachments if a.filename == 'AGENTS.md'), None)
            if not attachment:
                continue
            if self.store.meta.get('prompt_attachment') == attachment.id:
                return
            target = Path(os.environ.get('CODEX_PROMPT_TARGET') or codex_storage.codex_home()/'AGENTS.md')
            target.parent.mkdir(parents=True, exist_ok=True)
            data = await attachment.read()
            if target.exists():
                backup = target.with_name(target.name + f'.bak-{int(time.time())}')
                backup.write_bytes(target.read_bytes())
            temporary = target.with_name(target.name + '.tmp')
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb') as file:
                file.write(data)
            os.replace(temporary, target)
            self.store.meta['prompt_attachment'] = attachment.id
            self.store.save()
            return

    async def board_tick(self):
        if not upstream.BOARD or self.main_channel is None:
            return
        meta = self.store.meta
        if time.time() - meta.get('board_at', 0) < upstream.BOARD_MIN_SECS:
            return
        body, full = await self.frontend.board_for('codex')
        if body == meta.get('board_body') and time.time() - meta.get('board_at', 0) < 600:
            return
        message = None
        if meta.get('board_msg'):
            try:
                message = await self.main_channel.fetch_message(meta['board_msg'])
                await message.edit(content=full, allowed_mentions=upstream.NO_PING)
            except discord.HTTPException:
                message = None
        if message is None:
            message = await self.main_channel.send(full, allowed_mentions=upstream.NO_PING)
            meta['board_msg'] = message.id
            try:
                await message.pin(reason='signalscope board')
            except discord.HTTPException:
                pass  # A missing pin permission must not create a new board every tick.
        meta.update(board_body=body, board_at=time.time())
        self.store.save()

    def log_event(self, text):
        events = self.store.meta.setdefault('events', [])
        events.append({'ts': int(time.time()), 'text': text[:120]})
        del events[:-8]
        self.store.save()

    async def discover_once(self):
        known = set(self.store.sessions)
        subscribed = set(self.live.subscribed)
        await super().discover_once()
        for key in set(self.store.sessions) - known:
            self.log_event(f'🚀 {self.store.sessions[key].name} arrived')
            if upstream.ANNOUNCE_NEW:
                await self.frontend.say(self.main_channel, f'🚀 **{self.store.sessions[key].name}** → <#{key}>')
        for session in list(self.store.sessions.values()):
            if session.status != 'ended' and session.codex_thread in self.live.subscribed and session.codex_thread not in subscribed:
                await self.catch_up(session)

    async def observe_session(self, session, info):
        if info.get('cwd'):
            session.cwd = info['cwd']
        changed_name = bool(info.get('name') and info['name'] != session.name)
        if info.get('name'):
            session.name = info['name']
        collision = any(s is not session and s.status != 'ended' and s.name == session.name
                        for s in self.store.sessions.values())
        # Codex IDs are UUIDv7, so their leading digits are a shared timestamp.
        # Use the entropy-bearing tail when upstream's renderer needs a short suffix.
        short_id = (session.codex_thread or '')[-4:]
        title = upstream.thread_title(session.name, short_id, collision)
        if title != session.thread_title_cache:
            channel = await self.live_channel(session)
            if getattr(channel, 'name', None) != title:
                self.frontend.retitle(channel, title)
            session.thread_title_cache = title
            if changed_name:
                await self.frontend.say(channel, f'-# ✏️ renamed to **{session.name}**')
        self.store.save()

    async def catch_up(self, session):
        """Recover persisted replies missed during a bridge disconnect; never rerun a turn."""
        async with self.event_locks.setdefault(session.codex_thread, asyncio.Lock()):
            if not session.mirror_since:
                session.mirror_since = discord.utils.snowflake_time(session.discord_thread).timestamp()
            known = set(session.mirrored_turns)
            if not session.delivery_failed:
                known.update(k.removeprefix('completed:') for k in session.seen_live_items if k.startswith('completed:'))
            cursor, pending = None, []
            try:
                while True:
                    page = await self.live.call('thread/turns/list', {'threadId': session.codex_thread,
                        'limit': 10, 'sortDirection': 'desc', 'itemsView': 'summary', 'cursor': cursor})
                    reached_checkpoint = False
                    for turn in page['data']:
                        if turn['status'] == 'inProgress':
                            session.active_turn = turn['id']
                            continue
                        timestamp = turn.get('completedAt') or turn.get('startedAt') or 0
                        if not timestamp:
                            try:
                                identifier = uuid.UUID(turn['id'])
                                if identifier.version == 7:
                                    timestamp = int(identifier.hex[:12], 16) / 1000
                            except ValueError:
                                pass
                        if turn['id'] != session.active_turn and (not timestamp or timestamp + 1 < session.mirror_since):
                            reached_checkpoint = True
                            continue
                        if turn['id'] not in known:
                            pending.append(turn)
                    cursor = page.get('nextCursor')
                    if reached_checkpoint or not cursor:
                        break
                for turn in reversed(pending):
                    for item in turn.get('items', []):
                        if item.get('type') == 'agentMessage':
                            await self._handle_live_event({'method': 'item/completed', 'params': {
                                'threadId': session.codex_thread, 'turnId': turn['id'], 'item': item}})
                    if turn.get('error'):
                        await self.say(await self.live_channel(session), f'⚠️ Recovered failed turn: {turn["error"].get("message", "unknown error")}')
                    if turn['status'] == 'completed':
                        session.turns += 1
                    session.mirrored_turns = (session.mirrored_turns + [turn['id']])[-1000:]
                    session.seen_live_items = (session.seen_live_items + [f'completed:{turn["id"]}'])[-256:]
                    session.mirror_since = max(session.mirror_since, turn.get('completedAt') or session.mirror_since)
                    self.store.save()
                session.delivery_failed = False
                self.store.save()
            except Exception:
                session.delivery_failed = True
                self.store.save()
                raise

    async def show_request(self, session, event):
        from shared_prompts import request_view
        key = event.get('requestKey')
        request = self.live.server_requests.get(key)
        if request:
            session.status = 'waiting'
            self.store.save()
            body, view = request_view(self, key, request)
            await (await self.live_channel(session)).send(body, view=view, allowed_mentions=upstream.NO_PING)

    async def on_component(self, interaction):
        cid = (interaction.data or {}).get('custom_id', '')
        if not self.allowed(interaction.user.id, interaction.channel):
            return
        parts = cid.split('|')
        if len(parts) == 3 and parts[0] in {'o', 'k'} and parts[1].startswith('codex:'):
            session = self.store.sessions.get(interaction.channel_id)
            if not session or parts[1] != f'codex:{session.discord_thread}':
                return
            await interaction.response.defer()
            before = await self.terminal.screen(session)
            kind, _, key = parts
            if key != '__refresh':
                await self.terminal.key(session, key)
                if kind == 'o':
                    await asyncio.sleep(1.2)
                    after = await self.terminal.screen(session)
                    if upstream.parse_options(after)[0] and upstream.parse_options(after)[0] == upstream.parse_options(before)[0]:
                        await self.terminal.key(session, 'Enter')
            text = await self.terminal.screen(session)
            options = upstream.parse_options(text)[0] if session.status == 'waiting' else []
            await interaction.message.edit(content=upstream.prompt_body(text),
                view=upstream.prompt_view(parts[1], options), allowed_mentions=upstream.NO_PING)
            self.store.save()
            return
        if cid.startswith('rp|'):
            _, query, page = cid.split('|')
            rows = await self.search_sessions(query)
            await interaction.response.edit_message(content=upstream.resume_text(query, int(page), rows),
                                                    view=upstream.resume_view(query, int(page), rows))
            return
        if cid.startswith('rs|'):
            sid = (interaction.data.get('values') or [None])[0]
            await interaction.response.defer(thinking=True)
            if sid:
                thread = await self.start_session('', codex_id=sid)
                await interaction.followup.send(f'♻️ resumed → {thread.mention}', allowed_mentions=upstream.NO_PING)
            return
        if cid.startswith('cx|'):
            key = cid.split('|')[1]
            if key not in self.live.server_requests and not interaction.response.is_done():
                await interaction.response.send_message('That prompt expired. Use the newest prompt.', ephemeral=True)


class SimpleMessage:
    def __init__(self, channel, user, content):
        self.channel, self.author, self.content = channel, user, content
        self.attachments = []
    async def add_reaction(self, emoji):
        pass


def index_row(info):
    path = Path(info['path']) if info.get('path') else None
    return {**info, 'sid': info['id'], 'label': info.get('name') or info.get('preview', '')[:60] or info['id'][:8],
            'name': info.get('name') or '', 'first': info.get('preview') or '',
            'project': Path(info.get('cwd') or '.').name, 'cwd': info.get('cwd') or '',
            'mtime': info.get('updatedAt') or info.get('createdAt') or 0,
            'size': path.stat().st_size if path and path.exists() else 0,
            'where': 'local', 'live': (info.get('status') or {}).get('type') in {'active', 'idle'}}


class MessageWithAttachments:
    def __init__(self, message, content):
        self.original, self.content, self.attachments = message, content, []
    def __getattr__(self, name):
        return getattr(self.original, name)


def text_arguments(command, value):
    if command in {'status', 'threads', 'ls'}:
        return {'_command': 'sessions'}
    if command in {'bypass', 'auto'}: return {'_command': 'mode', 'mode': command}
    if command == 'unstick': return {'_command': 'refresh', 'force': value.startswith('force'), 'message': value.removeprefix('force').strip()}
    if command == 'revive' and value.strip() == 'all': return {'_command': 'reviveall'}
    if command in {'codex', 'astra'}: return {'prompt': value}
    if command == 'resume': return {'session': value}
    if command in {'model', 'globalmodel', 'rename'}: return {'name': value}
    if command == 'effort': return {'level': value}
    if command == 'key': return {'key': value}
    if command in {'all', 'hub'}: return {'message': value}
    if command == 'fork':
        words = value.split(maxsplit=1)
        if words and words[0] in {'codex', 'astra', 'claude'}:
            return {'to': words[0], 'message': words[1] if len(words) > 1 else ''}
        return {'to': 'same', 'message': value}
    if command == 'log': return {'count': int(value) if value.isdigit() else (200 if value == 'all' else 25)}
    if command in {'kill', 'revive'}: return {'how' if command == 'kill' else 'mode': value}
    if command == 'restart' and value.startswith('all'): return {'_command': 'restartall', 'force': 'force' in value}
    if command in {'restart', 'refresh'}: return {'force': value.startswith('force'), 'message': value.removeprefix('force').strip() if command == 'refresh' else ''}
    if command == 'cleanup': return {'delete': value == 'delete'}
    if command == 'fast': return {'mode': value.split()[0] if value else 'on', 'everywhere': 'everywhere' in value or 'all' in value}
    if command == 'mode': return {'mode': value}
    if command == 'yolo': return {'duration': value}
    if command == 'feldspar': return {'focus': value}
    if command in {'offload', 'restore'}: return {'directory': value.removesuffix(' confirm').strip(), 'confirm': value.endswith(' confirm')}
    if command == 'supernova':
        words = value.split()
        seconds = upstream.parse_duration(words[0]) if words and words[0] not in {'off', 'cancel'} else 22 * 60
        if seconds is None:
            raise ValueError('Use a duration such as 22m, 1h, or off.')
        return {'cancel': value in {'off', 'cancel'}, 'seconds': seconds,
                'then': words[1] if len(words) > 1 else 'wrap'}
    return {}
