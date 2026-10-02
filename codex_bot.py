"""Discord UI for the Codex backend. Run through `python chert.py`."""
import asyncio
from collections import deque
from dataclasses import dataclass
import fcntl
import io
import logging
import os
from pathlib import Path
import re
import shutil
import time
import uuid

import discord
from discord import app_commands

from codex_backend import CodexRunner, Session, SessionStore, project_path
from codex_live import LiveCodex, RpcError, discoverable, live_status
from codex_presentation import activity_text, avatar_url, prompt_name, speaker_name, thread_title

LOG = logging.getLogger(__name__)
NO_MENTIONS = discord.AllowedMentions.none()
EFFORTS = ('minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
HELP = (
    '**Chert · Codex**\n'
    'Type a prompt in the main channel to start a session; reply in its thread to continue.\n'
    'Existing Codex sessions appear automatically. `/codex prompt [project]` selects a project.\n'
    '`/sessions` lists sessions; `/resume session [project]` attaches a Codex session ID.\n'
    'In a session: `/stop`, `/kill`, `/rename name`, `/model name`, `/effort level`.\n'
    'Text alternatives: `!codex prompt`, `!sessions`, `!stop`, `!kill`, `!rename name`, '
    '`!model name`, `!effort level`, `!help`. No mention is needed.\n'
    'Replies received during a turn are queued. `/stop` clears the queue and stops the turn. '
    'Replies to a discovered live session steer its active turn directly. '
    'Model/effort changes apply next turn.'
)


@dataclass
class Config:
    token: str
    channel_id: int
    owner_id: int
    allowed_users: set[int]
    project_root: Path
    state_file: Path
    model: str = ''
    effort: str = ''
    discover: bool = True
    live_socket: Path | None = None
    discovery_interval: float = 5

    @classmethod
    def from_env(cls):
        token = os.environ.get('DISCORD_BOT_TOKEN', '').strip()
        channel = int(os.environ.get('DISCORD_CODEX_CHANNEL_ID') or os.environ.get('DISCORD_CHANNEL_ID') or 0)
        if not token or not channel:
            raise ValueError('Run setup_discord.py to set DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID.')
        root = Path(os.environ.get('PROJECT_ROOT') or str(Path.home() / 'projects')).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f'PROJECT_ROOT does not exist: {root}')
        return cls(token, channel, int(os.environ.get('DISCORD_OWNER_ID') or 0),
                   {int(x.strip()) for x in os.environ.get('SPAWN_ALLOW_USERS', '').split(',') if x.strip()},
                   root, Path(os.environ.get('CODEX_STATE_FILE') or 'private/codex-state.json'),
                   os.environ.get('CODEX_MODEL', '').strip(), os.environ.get('CODEX_EFFORT', '').strip(),
                   os.environ.get('CODEX_DISCOVER', '1') != '0',
                   Path(os.environ.get('CODEX_APP_SERVER_SOCKET') or
                        str(Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') /
                            'app-server-control/app-server-control.sock')).expanduser(),
                   max(1, float(os.environ.get('CODEX_DISCOVERY_INTERVAL') or 5)))


class CommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction):
        if not self.client.allowed(interaction.user.id, interaction.channel):
            await interaction.response.send_message('This Chert channel is restricted.', ephemeral=True)
            return False
        return True


class CodexBot(discord.Client):
    def __init__(self, config, runner, store, live=None, register_commands=True):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=NO_MENTIONS)
        self.config, self.runner, self.store = config, runner, store
        self.tree = CommandTree(self)
        self.main_channel = None
        self.owner = config.owner_id
        self.queues: dict[int, deque] = {}
        self.workers: dict[int, asyncio.Task] = {}
        self.stopping = set()
        self.session_creation_lock = asyncio.Lock()
        self.live = live or LiveCodex(config.live_socket or Path.home() / '.codex/app-server-control/app-server-control.sock')
        self.background_tasks = []
        self.webhook = None
        self.webhook_lock = asyncio.Lock()
        if register_commands:
            self.register_commands()

    def allowed(self, user_id, channel):
        in_scope = channel is not None and (
            channel.id == self.config.channel_id or
            isinstance(channel, discord.Thread) and channel.parent_id == self.config.channel_id)
        return bool(in_scope and (user_id == self.owner or user_id in self.config.allowed_users))

    async def setup_hook(self):
        self.main_channel = await self.fetch_channel(self.config.channel_id)
        if not isinstance(self.main_channel, discord.TextChannel):
            raise ValueError('DISCORD_CHANNEL_ID must point to a server text channel.')
        guild = await self.fetch_guild(self.main_channel.guild.id)
        self.owner = self.config.owner_id or guild.owner_id
        await self.webhook_for()
        # Guild-scoped registration makes commands available immediately in this server.
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.store.save()
        self.background_tasks = [asyncio.create_task(self.live_events())]
        if self.config.discover:
            self.background_tasks.append(asyncio.create_task(self.discover_loop()))

    async def on_ready(self):
        LOG.info('Codex bridge online as %s in #%s', self.user, self.main_channel.name)

    async def close(self):
        for task in self.background_tasks:
            task.cancel()
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
        await self.live.close()
        tasks = list(self.workers.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await super().close()

    async def discover_loop(self):
        await self.wait_until_ready()
        warned = False
        while not self.is_closed():
            try:
                await self.live.connect()
                await self.discover_once()
                warned = False
            except (OSError, ConnectionError, RpcError, asyncio.TimeoutError) as exc:
                if not warned:
                    LOG.warning('Codex live discovery unavailable; will retry: %s', exc)
                    warned = True
            except Exception:
                LOG.exception('Codex discovery failed; will retry')
            await asyncio.sleep(self.config.discovery_interval)

    async def discover_once(self):
        threads = await self.live.loaded_threads()
        loaded_ids = {t['id'] for t in threads}
        for info in threads:
            if not discoverable(info):
                continue
            async with self.session_creation_lock:
                session = next((s for s in self.store.sessions.values() if s.codex_thread == info['id']), None)
                if session is not None and session.status == 'ended':
                    if not session.ended_seen_absent:
                        continue  # Ignore a stale in-flight snapshot just after /kill.
                    # An external client explicitly resumed this conversation after
                    # it disappeared. Reopen its original Discord thread, as upstream does.
                    await self.live_channel(session)
                    session.status = live_status(info)
                    session.ended_seen_absent = False
                    self.store.save()
                if session is not None and session.discord_thread in self.workers:
                    continue  # Don't mirror our own exec output twice.
                if session is None:
                    title = info.get('name') or info.get('agentNickname') or Path(info['cwd']).name or 'Codex'
                    thread = await self.main_channel.create_thread(
                        name=thread_title(title), type=discord.ChannelType.public_thread,
                        auto_archive_duration=1440)
                    session = Session(thread.id, info['cwd'], title, info['id'],
                                      status=live_status(info), backend='app-server')
                    self.store.sessions[thread.id] = session
                    self.store.save()  # Record the mapping before subscribing to notifications.
                    card = await self.say(thread,
                        f'**Codex · {title}** · `{info["cwd"]}`\n'
                        f'**{session.status}** · Discovered an existing session. Reply here to talk to it; '
                        'your terminal and Discord share the same conversation.')
                    session.status_message = card.id
                    session.status_webhook = True
                    self.store.save()
                    LOG.info('Discovered Codex session %s → Discord thread %s', info['id'], thread.id)
                elif session.backend != 'app-server':
                    session.backend = 'app-server'
                    self.store.save()
                if info.get('model') and session.display_model != info['model']:
                    session.display_model = info['model']
                    self.store.save()
                await self.observe_session(session, info)
                if not session.status_webhook:
                    # Upgrade the old bot-authored status card once, without
                    # recreating the thread or replaying the conversation.
                    channel = await self.live_channel(session)
                    await channel.edit(name=thread_title(session.name))
                    card = await self.say(channel, activity_text(session, session.status, session.turn_started))
                    session.status_message, session.status_webhook = card.id, True
                    self.store.save()
                await self.live.attach(info['id'])
                if session.status != live_status(info):
                    session.status = live_status(info)
                    self.store.save()
                    await self.update_live_status(session)
        for session in list(self.store.sessions.values()):
            if session.status == 'ended' and session.codex_thread not in loaded_ids and not session.ended_seen_absent:
                session.ended_seen_absent = True
                self.store.save()
            if session.backend == 'app-server' and session.status != 'ended' and session.codex_thread not in loaded_ids:
                if session.status != 'disconnected':
                    session.status = 'disconnected'
                    self.store.save()
                    await self.update_live_status(session)
                self.live.subscribed.discard(session.codex_thread)

    async def observe_session(self, session, info):
        return None

    async def live_channel(self, session):
        channel = self.get_channel(session.discord_thread) or await self.fetch_channel(session.discord_thread)
        if channel.archived:
            await channel.edit(archived=False)
        return channel

    async def update_live_status(self, session):
        if session.status_message is None:
            return
        channel = await self.live_channel(session)
        content = activity_text(session, session.status, session.turn_started)
        if session.status_webhook:
            webhook = await self.webhook_for()
            await webhook.edit_message(session.status_message, content=content,
                                       thread=channel, allowed_mentions=NO_MENTIONS)
        else:
            await channel.get_partial_message(session.status_message).edit(
                content=content, allowed_mentions=NO_MENTIONS)

    async def live_events(self):
        await self.wait_until_ready()
        while not self.is_closed():
            event = await self.live.notifications.get()
            try:
                await self.handle_live_event(event)
            except Exception:
                LOG.exception('Could not mirror Codex event to Discord')
            finally:
                self.live.notifications.task_done()

    async def handle_live_event(self, event):
        method, params = event['method'], event.get('params') or {}
        session = next((s for s in self.store.sessions.values()
                        if s.backend == 'app-server' and s.codex_thread == params.get('threadId')
                        and s.status != 'ended'), None)
        if session is None:
            return
        if method == 'item/completed':
            item = params.get('item') or {}
            key = f'{params.get("turnId", "")}:{item.get("id", "")}'
            if item.get('type') != 'agentMessage' or not item.get('text') or key in session.seen_live_items:
                return
            await self.say(await self.live_channel(session), item['text'])
            session.seen_live_items = (session.seen_live_items + [key])[-256:]
            self.store.save()
        elif method in {'turn/started', 'turn/completed'}:
            turn = params['turn']
            if method == 'turn/started':
                session.active_turn, session.status = turn['id'], 'running'
                session.turn_started = time.time()
            else:
                key = f'completed:{turn["id"]}'
                if key in session.seen_live_items:
                    return
                session.seen_live_items = (session.seen_live_items + [key])[-256:]
                if turn['status'] == 'completed':
                    session.turns += 1
                session.active_turn = None
                session.status = 'error' if turn['status'] == 'failed' else 'idle'
                if turn.get('error'):
                    await self.say(await self.live_channel(session), f'Codex turn failed: {turn["error"].get("message", "unknown error")}')
            self.store.save()
            await self.update_live_status(session)
        elif method == 'chert/inputRequired':
            await self.say(await self.live_channel(session), 'Codex needs approval or input in the original Codex client.')
        elif method == 'thread/status/changed':
            session.status = live_status({'status': params['status']})
            self.store.save()
            await self.update_live_status(session)

    async def webhook_for(self):
        async with self.webhook_lock:
            if self.webhook is None:
                hooks = await self.main_channel.webhooks()
                self.webhook = next((w for w in hooks if w.name == 'chert-codex' and w.token), None)
                if self.webhook is None:
                    self.webhook = await self.main_channel.create_webhook(name='chert-codex')
            return self.webhook

    async def say(self, channel, text):
        text = str(text).strip() or '(No text returned.)'
        session = self.store.sessions.get(channel.id)

        async def send(content, **kwargs):
            if session is None:
                return await channel.send(content, **kwargs, allowed_mentions=NO_MENTIONS, suppress_embeds=True)
            webhook = await self.webhook_for()
            try:
                return await webhook.send(content, **kwargs, thread=channel,
                                          username=speaker_name(session), avatar_url=avatar_url(session),
                                          allowed_mentions=NO_MENTIONS, suppress_embeds=True, wait=True)
            except discord.NotFound:
                # A deleted webhook can be recreated without losing a session.
                self.webhook = None
                webhook = await self.webhook_for()
                return await webhook.send(content, **kwargs, thread=channel,
                                          username=speaker_name(session), avatar_url=avatar_url(session),
                                          allowed_mentions=NO_MENTIONS, suppress_embeds=True, wait=True)

        if len(text) > 12000:
            return await send('Response attached.', file=discord.File(
                io.BytesIO(text.encode()), filename='codex-response.txt'))
        result = None
        for offset in range(0, len(text), 1900):
            result = await send(text[offset:offset + 1900])
        return result

    async def start_session(self, prompt, project='', codex_id=None, source_message=None):
        # Two simultaneous /resume commands must not attach the same Codex ID twice.
        async with self.session_creation_lock:
            return await self._start_session(prompt, project, codex_id, source_message)

    async def _start_session(self, prompt, project='', codex_id=None, source_message=None):
        cwd = project_path(self.config.project_root, project)
        if source_message and source_message.id in self.store.sessions:
            return await self.fetch_channel(source_message.id)
        if codex_id:
            codex_id = str(uuid.UUID(codex_id))
            old = next((s for s in self.store.sessions.values() if s.codex_thread == codex_id), None)
            if old:
                thread = await self.fetch_channel(old.discord_thread)
                await thread.edit(archived=False)
                if old.status == 'ended':
                    old.status = 'idle'
                    self.store.save()
                return thread
        title = prompt_name(prompt) if prompt else f'codex-{codex_id[:8]}'
        if source_message:
            await source_message.add_reaction('🚀')
            try:
                thread = await source_message.create_thread(name=thread_title(title), auto_archive_duration=10080)
            except discord.HTTPException:
                await source_message.add_reaction('❌')
                raise
        else:
            thread = await self.main_channel.create_thread(name=thread_title(title), type=discord.ChannelType.public_thread,
                                                           auto_archive_duration=10080)
        session = Session(thread.id, str(cwd), title, codex_id,
                          self.config.model, self.config.effort)
        session.source_message = source_message.id if source_message else None
        self.store.sessions[thread.id] = session
        self.store.save()
        await self.say(thread, f'**{title}** · `{cwd.name}`\nReply to talk · `!stop` · `!help`')
        if prompt:
            if source_message is None:
                await self.say(thread, prompt)
            self.enqueue(thread, prompt)
            if source_message:
                await source_message.add_reaction('📡')
        return thread

    async def launch_message(self, message, prompt):
        project = ''
        words = prompt.split(maxsplit=1)
        if len(words) == 2:
            first, rest = words
            try:
                project_path(self.config.project_root, first)
            except ValueError:
                pass
            else:
                project, prompt = first, rest.strip()
        if message.channel.id == self.config.channel_id:
            await self.start_session(prompt, project=project, source_message=message)
        else:
            thread = await self.start_session(prompt, project=project)
            await self.say(message.channel, f'Continue in {thread.mention}')

    def enqueue(self, thread, prompt):
        session = self.store.sessions[thread.id]
        if thread.id in self.stopping:
            raise ValueError('This turn is stopping; send your message again once it stops.')
        if session.status == 'ended':
            raise ValueError('Session ended. Use /resume with its Codex session ID to reopen it.')
        queue = self.queues.setdefault(thread.id, deque())
        if len(queue) >= 20:
            raise ValueError('This session already has 20 queued messages. Wait or use /stop.')
        queue.append(prompt)
        queued = thread.id in self.workers
        if not queued:
            self.workers[thread.id] = asyncio.create_task(self.work(thread, session))
        return queued

    async def send_prompt(self, thread, prompt):
        session = self.store.sessions[thread.id]
        if session.status == 'ended':
            raise ValueError('Session ended. Use /resume to reopen it.')
        if session.backend == 'app-server':
            if thread.id in self.stopping:
                raise ValueError('This session is stopping; try again once it stops.')
            result = await self.live.submit(session, prompt)
            session.status = 'running'
            self.store.save()
            return '↪️' if result == 'steered' else '👀'
        return '⏳' if self.enqueue(thread, prompt) else '🤔'

    async def work(self, thread, session):
        queue = self.queues[thread.id]
        try:
            while queue:
                prompt = queue.popleft()
                session.status = 'running'
                session.turn_started = time.time()
                self.store.save()
                card = await self.say(thread, activity_text(session, 'running', session.turn_started))
                last_edit = 0.0

                async def on_event(event):
                    nonlocal last_edit
                    if event.get('type') == 'thread.started':
                        self.store.save()  # Resume survives a bridge restart mid-turn.
                    item = event.get('item') or {}
                    kind = item.get('type')
                    label = {'command_execution': 'Running a command', 'file_change': 'Editing files',
                             'web_search': 'Searching the web', 'mcp_tool_call': 'Using a tool',
                             'agent_message': 'Writing a response'}.get(kind)
                    if label and time.monotonic() - last_edit > 3:
                        last_edit = time.monotonic()
                        try:
                            await card.edit(content=activity_text(session, 'running', session.turn_started, label),
                                            allowed_mentions=NO_MENTIONS)
                        except discord.HTTPException:
                            LOG.warning('Could not update progress in %s', thread.id)

                result = await self.runner.run(session, prompt, on_event)
                session.status = 'idle' if result.ok else 'error'
                if result.ok:
                    session.turns += 1
                self.store.save()
                tokens = result.usage.get('output_tokens')
                summary = activity_text(session, session.status, session.turn_started,
                                        f'{tokens} output tokens' if tokens is not None else '')
                await card.edit(content=summary, allowed_mentions=NO_MENTIONS)
                if result.text:
                    await self.say(thread, result.text)
                if result.errors:
                    await self.say(thread, '\n'.join(result.errors))
                    if queue:
                        await self.say(thread, 'Queued messages cleared after the failed turn. Please resend them.')
                    break  # Never automatically retry a turn that may already have changed files.
        except asyncio.CancelledError:
            session.status = 'interrupted'
            raise
        except Exception:
            session.status = 'error'
            LOG.exception('Turn failed in Discord thread %s', thread.id)
            try:
                await self.say(thread, 'Turn failed; queued messages cleared. See the bridge log for details.')
            except discord.HTTPException:
                pass
        finally:
            queue.clear()
            self.workers.pop(thread.id, None)
            self.queues.pop(thread.id, None)
            self.store.save()

    async def stop(self, thread, end=False):
        session = self.store.sessions[thread.id]
        if thread.id in self.stopping:
            raise ValueError('This session is already stopping.')
        self.stopping.add(thread.id)
        try:
            if session.backend == 'app-server':
                await self.live.interrupt(session.codex_thread)
            worker = self.workers.pop(thread.id, None)
            if worker:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            self.queues.pop(thread.id, None)
            session.status = 'ended' if end else 'idle'
            if end:
                session.ended_seen_absent = False
                session.ended_at = time.time()
            self.store.save()
            await self.say(thread, 'Session ended.' if end else 'Stopped. Queued messages cleared; reply to continue.')
            if end:
                await thread.edit(name=thread_title(session.name, ended=True), archived=True)
        finally:
            self.stopping.discard(thread.id)

    def session_list(self):
        rows = [f'<#{s.discord_thread}> · **{s.status}** · {s.turns} turns'
                + (f' · `{s.codex_thread}`' if s.codex_thread else '')
                for s in self.store.sessions.values()]
        return '\n'.join(rows[-40:]) or 'No sessions yet. Type a prompt in the main channel to start.'

    async def control(self, channel, command, value=''):
        if command == 'help':
            return await self.say(channel, HELP)
        if command == 'sessions':
            return await self.say(channel, self.session_list())
        session = self.store.sessions.get(channel.id)
        if not session:
            raise ValueError('Use this command inside a Chert Codex session thread.')
        if command in {'stop', 'kill'}:
            return await self.stop(channel, end=command == 'kill')
        if command == 'rename':
            if not value.strip():
                raise ValueError('Provide a name.')
            session.name = value.strip()[:90]
            await channel.edit(name=thread_title(session.name))
        elif command == 'model':
            if value == 'default':
                session.model = ''
            elif re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}', value):
                session.model = value
            else:
                raise ValueError('Provide a model name, or default to use your Codex configuration.')
        elif command == 'effort':
            if value not in (*EFFORTS, 'default'):
                raise ValueError('Choose an effort supported by your model: ' + ', '.join(EFFORTS) + ', default.')
            session.effort = '' if value == 'default' else value
        else:
            raise ValueError('Unknown command. Use !help.')
        self.store.save()
        await self.say(channel, f'{command.capitalize()} set to `{value}` (applies to the next turn).')

    def register_commands(self):
        @self.tree.command(name='codex', description='Start a Codex session in its own thread')
        async def codex(interaction: discord.Interaction, prompt: str, project: str = ''):
            await interaction.response.defer(ephemeral=True)
            thread = await self.start_session(prompt, project)
            await interaction.followup.send(f'Continue in {thread.mention}', ephemeral=True)

        @self.tree.command(name='resume', description='Attach or reopen a Codex session by UUID')
        async def resume(interaction: discord.Interaction, session: str, project: str = ''):
            await interaction.response.defer(ephemeral=True)
            thread = await self.start_session('', project, session)
            await interaction.followup.send(f'Continue in {thread.mention}', ephemeral=True)

        # Factories keep command names and callback signatures explicit for discord.py.
        def simple(name, description):
            async def callback(interaction: discord.Interaction):
                await interaction.response.defer(ephemeral=True)
                await self.control(interaction.channel, name)
                await interaction.followup.send('Done.', ephemeral=True)
            self.tree.add_command(app_commands.Command(name=name, description=description, callback=callback))

        def setting(name, description):
            async def callback(interaction: discord.Interaction, value: str):
                await interaction.response.defer(ephemeral=True)
                await self.control(interaction.channel, name, value)
                await interaction.followup.send('Done.', ephemeral=True)
            self.tree.add_command(app_commands.Command(name=name, description=description, callback=callback))

        for name, description in [('stop', 'Stop the active turn and clear queued messages'),
                                  ('kill', 'End this session and archive its thread'),
                                  ('sessions', 'List Codex sessions and their IDs'),
                                  ('help', 'Show Chert commands')]:
            simple(name, description)
        for name, description in [('model', 'Set the model for subsequent turns'),
                                  ('effort', 'Set reasoning effort for subsequent turns'),
                                  ('rename', 'Rename this session thread')]:
            setting(name, description)

        @self.tree.error
        async def on_error(interaction, error):
            cause = getattr(error, 'original', error)
            if isinstance(cause, (ValueError, RpcError)):
                message = str(cause)
            else:
                LOG.error('Discord command failed: %s', error, exc_info=error)
                message = 'Command failed. Check the bridge log for details.'
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)

    async def on_message(self, message):
        if message.author.bot or getattr(message, 'webhook_id', None) or not self.allowed(message.author.id, message.channel):
            return
        content = message.content.strip()
        try:
            if content.startswith('!'):
                command, _, value = content[1:].partition(' ')
                if command == 'codex':
                    if not value.strip():
                        raise ValueError('Use !codex followed by a prompt.')
                    await self.launch_message(message, value)
                else:
                    await self.control(message.channel, command, value.strip())
                return
            if message.channel.id in self.store.sessions:
                if message.attachments:
                    await self.say(message.channel, 'Attachments are not imported. Put files in the project directory and include their paths.')
                if content:
                    # Preserve attribution for explicitly allowed collaborators.
                    if message.author.id != self.owner:
                        content = f'{message.author.display_name}: {content}'
                    reaction = await self.send_prompt(message.channel, content)
                    await message.add_reaction(reaction)
            elif message.channel.id == self.config.channel_id:
                prompt = re.sub(rf'<@!?{self.user.id}>', '', content).strip() if self.user else content
                if message.attachments:
                    await self.say(message.channel, 'Attachments are not imported. Put files in the project directory and include their paths.')
                if prompt:
                    await self.launch_message(message, prompt)
        except (ValueError, RpcError) as exc:
            await self.say(message.channel, str(exc))
        except discord.HTTPException:
            LOG.exception('Could not create or update the Discord session')
            await self.say(message.channel, 'Could not open the session thread. Check the bot’s thread and webhook permissions.')
        except (OSError, ConnectionError, asyncio.TimeoutError):
            await self.say(message.channel,
                'The Codex connection was interrupted. Check the original session before resending; '
                'your message may already have reached it.')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    try:
        config = Config.from_env()
        binary = os.environ.get('CODEX_BIN') or shutil.which('codex') or str(Path.home() / '.local/bin/codex')
        if not shutil.which(binary):
            raise ValueError('Codex CLI not found. Install it and run codex login as the service user.')
        runner = CodexRunner(binary, os.environ.get('CODEX_SANDBOX') or 'workspace-write',
                             float(os.environ.get('CODEX_TURN_TIMEOUT') or 10800),
                             Path(os.environ.get('CODEX_LOG_DIR') or 'private/codex-logs'),
                             os.environ.get('CODEX_NETWORK_ACCESS', '0') == '1')
        config.state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A second bridge must not consume messages or overwrite session state.
        with open(str(config.state_file) + '.lock', 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Another Codex bridge is already using this state file.') from None
            store = SessionStore(config.state_file)
            CodexBot(config, runner, store).run(config.token, log_handler=None)
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name('.env'))
    main()
