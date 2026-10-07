"""Project-oriented Discord UI over the existing Codex and Claude transports."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import os
from pathlib import Path

import discord

import discord_bot as upstream
from projects import ProjectStore, create_project_channel
from shared_frontend import SharedFrontend

LOG = logging.getLogger(__name__)


class ProjectFrontend(SharedFrontend):
    def __init__(self, config, options, store, claude_channel_id=0, projects=None):
        self.projects = projects or ProjectStore(os.environ.get('PROJECT_STATE_FILE') or 'private/projects.json')
        self._project_context = ContextVar('chert_project', default=None)
        self.project_lock = asyncio.Lock()
        self.project_channels = {}
        super().__init__(config, options, store, claude_channel_id)
        self.claude_enabled = os.environ.get('CLAUDE_ENABLED', '1') != '0'
        self.install_project_commands()

    @property
    def current_project(self):
        return self._project_context.get()

    @contextmanager
    def project_context(self, project):
        token = self._project_context.set(project)
        try:
            yield
        finally:
            self._project_context.reset(token)

    @property
    def main_channel(self):
        project = self.current_project
        if project:
            return self.project_channels.get(project.channel_id)
        return getattr(self, '_main_channel', None)

    @main_channel.setter
    def main_channel(self, value):
        self._main_channel = value

    def backend_for(self, channel):
        project = self.projects.for_channel(channel)
        if project is None:
            return None
        if channel.id in self.codex.store.sessions:
            return 'codex'
        if channel.id in upstream.thread_to_key():
            return 'claude'
        # Unknown threads must never silently become a new session in another harness.
        return None if getattr(channel, 'parent_id', None) else project.harness

    async def setup_hook(self):
        guild_id = self.projects.guild_id or int(os.environ.get('DISCORD_GUILD_ID') or 0)
        if not guild_id:
            raise ValueError('Run setup_discord.py to configure project channels.')
        guild = await self.fetch_guild(guild_id)
        self.owner = self.codex.owner = self.codex.config.owner_id or guild.owner_id
        for project in self.projects.projects.values():
            try:
                channel = await self.fetch_channel(project.channel_id)
            except discord.NotFound:
                LOG.warning('Project channel %s was deleted; run setup_discord.py to repair it.', project.name)
                continue
            if channel.guild.id != guild_id:
                raise ValueError(f'Project {project.name} is in another server.')
            self.project_channels[channel.id] = channel
        self.main_channel = next((self.project_channels[p.channel_id] for p in self.projects.projects.values()
                                  if not p.archived and p.channel_id in self.project_channels),
                                 next(iter(self.project_channels.values()), None))
        await self.codex.start_backend()
        await self.start_hook_server()
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.poller = asyncio.create_task(self.poll_loop())

    async def on_ready(self):
        LOG.info('Project frontend online: %s projects in guild %s', len(self.projects.projects), self.projects.guild_id)

    async def poll_loop(self):
        """Poll Claude without upstream's auto-created harness channels or chat buses."""
        await self.wait_until_ready()
        if self.claude_enabled:
            upstream.migrate_state(await asyncio.to_thread(upstream.live_sessions))
            meta = upstream.state['_meta']
            boot = upstream.boot_id()
            self._rebooted = bool(boot and meta.get('boot_id') and boot != meta['boot_id'])
            if boot:
                meta['boot_id'] = boot
                upstream.save_state()
        backlog, self._hook_backlog = self._hook_backlog, []
        for event in backlog:
            await self.on_hook(event)
        while not self.is_closed():
            steps = [self.disk_tick, self.update_shared_presence]
            if self.claude_enabled:
                steps += [self.run_tick, self.drain_spool, self.supernova_tick]
            for step in steps:
                try:
                    await step()
                except Exception:
                    LOG.exception('Project monitor failed: %s', step.__name__)
            await asyncio.sleep(upstream.POLL_SECS)

    async def open_thread(self, channel, session, key):
        project = self.projects.for_directory(session.get('cwd'))
        if project is None or project.archived or project.channel_id not in self.project_channels:
            return
        async with self.project_lock:
            with self.project_context(project):
                return await super().open_thread(self.main_channel, session, key)

    async def tick_session(self, channel, key, session, saved):
        project = self.projects.for_directory(saved.get('cwd') or session.get('cwd'))
        with self.project_context(project):
            return await super().tick_session(self.main_channel or channel, key, session, saved)

    async def route_message(self, message):
        project = self.projects.for_channel(message.channel)
        if project is None:
            return
        if project.archived:
            return await self.say(message.channel, 'This project is archived. Use /unarchive to continue.')
        with self.project_context(project):
            content = (message.content or '').strip()
            command = content.split(maxsplit=1)[0].lower() if content else ''
            if command in {'!sessions', '!status', '!threads', '!ls'}:
                return await self.say(message.channel, self.project_sessions(project))
            if command in {'!all', '!hub', '!restartall', '!reviveall', '!cleanup'} or content.lower().startswith(('!restart all', '!revive all')):
                return await self.say(message.channel, 'Harness-wide commands have been removed. Use the session’s thread to control it.')
            if message.channel.id == project.channel_id:
                for mention in (f'<@{self.user.id}>', f'<@!{self.user.id}>'):
                    content = content.replace(mention, '')
                content = content.strip()
                harness = project.harness
                for prefix, selected in (('!codex ', 'codex'), ('!astra ', 'codex'), ('!claude ', 'claude')):
                    if content.startswith(prefix):
                        harness, content = selected, content[len(prefix):].strip()
                        break
                else:
                    if content.startswith('!'):
                        if harness == 'codex':
                            return await self.codex.on_message(message)
                        if not self.claude_enabled and command not in {'!help', '!disk', '!s3', '!backup', '!offload', '!restore'}:
                            return await self.say(message.channel, 'Claude is disabled. Use /harness codex.')
                        return await self.handle_main_command(message, content)
                try:
                    return await self.launch_project_message(project, harness, message, content)
                except Exception as exc:
                    LOG.exception('Project session launch failed')
                    return await self.say(message.channel, f'Could not launch: {str(exc)[:1500]}')
            return await super().route_message(message)

    async def launch_project_message(self, project, harness, message, prompt):
        attached = await self.save_attachments(message)
        prompt = '\n'.join(part for part in (prompt, attached) if part)
        if not prompt:
            return
        if harness == 'codex':
            async with self.codex.session_creation_lock:
                return await self.codex._start_session(prompt, source_message=message, cwd=Path(project.directory))
        return await self.launch_claude(project, prompt, message.author,
                                       lambda text: self.say(message.channel, text), source=message)

    async def launch_claude(self, project, prompt, user, respond, source=None):
        if not self.claude_enabled:
            return await respond('Claude is disabled. Use /harness codex or enable Claude on the bot host.')
        async with self.project_lock:
            if source:
                await source.add_reaction('🚀')
            pane, error = await asyncio.to_thread(upstream.spawn_claude, upstream.slug(prompt, 32), project.directory)
            if not pane:
                return await respond(f'Could not launch Claude: {error}')
            session, notes = await self.await_registration(pane)
            if not session:
                return await respond(await self.spawn_failure_text(pane, project.directory, notes))
            # Both launch and discovery use this lock so only one owns thread creation.
            if source:
                thread = await self.claude.adopt_attached(session, source)
            else:
                thread = await self.adopt_session(session, self.main_channel, {'spawned': True})
            ok, error = await asyncio.to_thread(self.deliver, session, user, prompt)
            if source and ok:
                await source.add_reaction('📡')
            elif not source:
                await respond(f'Claude → {thread.mention}' if thread else 'Claude launched; waiting for its thread.')
            if not ok:
                await respond(f'Prompt was not delivered: {error}')
            return thread

    async def route_command(self, name, original, interaction, kwargs):
        project = self.projects.for_channel(interaction.channel)
        if project is None:
            return await interaction.response.send_message('Use this command in a project channel. /project creates one.', ephemeral=True)
        if project.archived and name not in {'stop', 'key', 'kill', 'log', 'screen', 'help'}:
            return await interaction.response.send_message('This project is archived. Use /unarchive to continue.', ephemeral=True)
        with self.project_context(project):
            if name == 'sessions':
                return await interaction.response.send_message(self.project_sessions(project), allowed_mentions=upstream.NO_PING)
            if name in {'claude', 'codex', 'astra'}:
                if kwargs.get('project'):
                    return await interaction.response.send_message('The channel selects the project directory. Use that project’s channel.', ephemeral=True)
                await interaction.response.defer(thinking=True)
                async def respond(text):
                    return await interaction.followup.send(text, allowed_mentions=upstream.NO_PING)
                if name == 'claude':
                    return await self.launch_claude(project, kwargs['prompt'], interaction.user, respond)
                thread = await self.codex.start_session(kwargs['prompt'])
                return await respond(f'Codex → {thread.mention}')
            return await super().route_command(name, original, interaction, kwargs)

    def project_sessions(self, project):
        lines = [f'**{project.name}** · default harness: **{project.harness}**']
        for session in self.codex.store.sessions.values():
            if self.projects.for_directory(session.cwd) == project:
                lines.append(f'Codex · {session.status} · **{session.name}** → <#{session.discord_thread}>')
        for saved in upstream.sessions_state().values():
            if saved.get('parent') == project.channel_id and saved.get('thread'):
                status = 'ended' if saved.get('ended') else saved.get('status', 'idle')
                lines.append(f'Claude · {status} · **{saved.get("name", "session")}** → <#{saved["thread"]}>')
        return '\n'.join(lines)[:1900] if len(lines) > 1 else lines[0] + '\nNo sessions yet. Type a prompt to start one.'

    async def deliver_session_file(self, path, caption, pane, sid, cwd, backend=None):
        session = next((s for s in self.codex.store.sessions.values() if sid and s.codex_thread == sid), None)
        project = self.projects.for_directory(session.cwd if session else cwd)
        if project is None:
            raise ValueError('The session directory does not belong to a registered project.')
        with self.project_context(project):
            return await super().deliver_session_file(path, caption, pane, sid, cwd, backend)

    async def on_interaction(self, interaction):
        project = self.projects.for_channel(interaction.channel)
        with self.project_context(project):
            return await super().on_interaction(interaction)

    async def project_permission(self, interaction):
        if interaction.guild_id != self.projects.guild_id or not self.allowed_user(interaction.user):
            await interaction.response.send_message('This Chert instance is restricted.', ephemeral=True)
            return False
        return True

    async def category(self, archived=False):
        key = 'archive_category_id' if archived else 'category_id'
        category_id = getattr(self.projects, key)
        if category_id:
            return self.get_channel(category_id) or await self.fetch_channel(category_id)
        raise ValueError('Project categories are missing. Run setup_discord.py again.')

    async def create_project(self, name, directory):
        async with self.project_lock:
            project = self.projects.prepare(name, directory, self.codex.config.project_root,
                                            os.environ.get('DEFAULT_HARNESS') or 'codex')
            category = await self.category()
            channel = await create_project_channel(self.projects, project, category.guild, category)
            self.project_channels[channel.id] = channel
            if self._main_channel is None:
                self.main_channel = self.codex.main_channel = channel
            return project

    async def set_archived(self, project, archived):
        async with self.project_lock:
            channel = self.project_channels.get(project.channel_id) or await self.fetch_channel(project.channel_id)
            channel = await channel.edit(category=await self.category(archived),
                                         reason='Project archived' if archived else 'Project reopened')
            project.archived = archived
            self.project_channels[project.channel_id] = channel
            self.projects.save()

    async def set_harness(self, project, harness):
        if harness not in {'codex', 'claude'}:
            raise ValueError('Choose codex or claude.')
        async with self.project_lock:
            previous = project.harness
            project.harness = harness
            try:
                self.projects.save()
            except Exception:
                project.harness = previous
                raise

    def install_project_commands(self):
        # The launch command's directory is fixed by its project channel.
        for name in ('claude', 'codex', 'astra'):
            command = self.tree.get_command(name)
            command.description = f'Launch a {"Codex" if name != "claude" else "Claude"} session in this project'
            command._params.pop('project', None)
            command._params['prompt'].description = 'What the agent should do in this project'
        # These old harness-wide operations no longer describe the project UI.
        for name in ('all', 'hub', 'restartall', 'reviveall', 'cleanup'):
            self.tree.remove_command(name)
        @self.tree.command(name='project', description='Create a project channel for a directory on the bot host')
        async def project_command(interaction: discord.Interaction, name: str, dir: str):
            if not await self.project_permission(interaction):
                return
            await interaction.response.defer(ephemeral=True)
            project = await self.create_project(name, dir)
            await interaction.followup.send(f'Created <#{project.channel_id}> · `{project.directory}` · {project.harness}', ephemeral=True)

        @self.tree.command(name='harness', description='Choose the default harness for new sessions in this project')
        @discord.app_commands.choices(name=[discord.app_commands.Choice(name='Codex', value='codex'),
                                          discord.app_commands.Choice(name='Claude', value='claude')])
        async def harness_command(interaction: discord.Interaction, name: str = ''):
            if not await self.project_permission(interaction):
                return
            project = self.projects.for_channel(interaction.channel)
            if project is None:
                return await interaction.response.send_message('Use /harness in a project channel.', ephemeral=True)
            if not name:
                view = HarnessPicker(self, project, interaction.user.id)
                return await interaction.response.send_message(f'Default harness: **{project.harness}**. Choose a harness for new sessions.', view=view, ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            await self.set_harness(project, name)
            await interaction.followup.send(f'New sessions in <#{project.channel_id}> use **{name}**. Existing threads keep their harness.', ephemeral=True)

        async def archive_command(interaction, name, archived):
            if not await self.project_permission(interaction):
                return
            project = self.projects.projects.get(name) if name else self.projects.for_channel(interaction.channel)
            if project is None:
                return await interaction.response.send_message('Choose a project name or run this in its channel.', ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            await self.set_archived(project, archived)
            await interaction.followup.send(f'{"Archived" if archived else "Reopened"} <#{project.channel_id}>.', ephemeral=True)

        @self.tree.command(name='archive', description='Move this project to the archived section')
        async def archive(interaction: discord.Interaction, name: str = ''):
            await archive_command(interaction, name, True)

        @self.tree.command(name='unarchive', description='Move a project back to the projects section')
        async def unarchive(interaction: discord.Interaction, name: str = ''):
            await archive_command(interaction, name, False)

        for command in (archive, unarchive):
            @command.autocomplete('name')
            async def names(interaction, current):
                return [discord.app_commands.Choice(name=p.name, value=p.name)
                        for p in self.projects.projects.values() if current.lower() in p.name][:25]


class HarnessPicker(discord.ui.View):
    def __init__(self, frontend, project, user_id):
        super().__init__(timeout=120)
        self.frontend, self.project, self.user_id = frontend, project, user_id
        select = discord.ui.Select(options=[discord.SelectOption(label=h.title(), value=h,
                                  default=h == project.harness) for h in ('codex', 'claude')])
        select.callback = self.choose
        self.select = select
        self.add_item(select)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('Open your own /harness picker.', ephemeral=True)
            return False
        return await self.frontend.project_permission(interaction)

    async def choose(self, interaction):
        await interaction.response.defer()
        await self.frontend.set_harness(self.project, self.select.values[0])
        await interaction.edit_original_response(content=f'Default harness: **{self.project.harness}**. Existing threads keep their harness.', view=None)
        self.stop()
