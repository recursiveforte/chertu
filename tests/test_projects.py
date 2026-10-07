import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
import discord_bot as upstream
from codex_backend import Session, SessionStore
from config import Config
from project_frontend import ProjectFrontend
from projects import Project, ProjectStore
import setup_discord


class ProjectStoreTests(unittest.TestCase):
    def test_persistence_and_deepest_directory_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = root / 'child'
            child.mkdir()
            store = ProjectStore(root / 'projects.json')
            store.guild_id = 123
            store.projects = {'root': Project('root', tmp, 1),
                              'child': Project('child', str(child), 2, 'claude', True)}
            store.save()
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            loaded = ProjectStore(store.path)
            self.assertEqual(loaded.guild_id, 123)
            self.assertEqual(loaded.for_directory(child / 'src').name, 'child')
            self.assertTrue(loaded.projects['child'].archived)
            self.assertEqual(loaded.for_channel(SimpleNamespace(id=99, parent_id=2)).harness, 'claude')
            self.assertIsNone(loaded.for_directory(root.parent / 'elsewhere'))

    def test_directory_validation_does_not_create_or_reuse_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ProjectStore(Path(tmp) / 'projects.json')
            self.assertEqual(store.validate('Hello', '.', tmp), ('hello', str(Path(tmp).resolve())))
            for name, directory in [('bad name', '.'), ('valid', 'missing')]:
                with self.assertRaises(ValueError):
                    store.validate(name, directory, tmp)
            store.projects['hello'] = Project('hello', tmp, 1)
            with self.assertRaises(ValueError):
                store.validate('another', tmp, tmp)


class ProjectFrontendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.projects = ProjectStore(self.root / 'projects.json')
        self.projects.guild_id = 1
        self.projects.projects = {'one': Project('one', str(self.root / 'one'), 100),
                                  'two': Project('two', str(self.root / 'two'), 200, 'claude')}
        for project in self.projects.projects.values():
            Path(project.directory).mkdir()
        config = Config('test', 0, 7, set(), self.root, self.root / 'codex.json')
        self.bot = ProjectFrontend(config, SimpleNamespace(binary='codex', sandbox='workspace-write', network=False), SessionStore(config.state_file), projects=self.projects)
        self.bot.owner = self.bot.codex.owner = 7
        self.bot._connection.user = SimpleNamespace(id=1)
        self.channels = {i: SimpleNamespace(id=i, parent_id=None, guild=SimpleNamespace(id=1),
                         edit=AsyncMock(), create_thread=AsyncMock()) for i in (100, 200)}
        for channel in self.channels.values():
            channel.edit.return_value = channel
        self.bot.project_channels = self.channels
        self.bot.main_channel = self.bot.codex.main_channel = self.channels[100]
        self.bot.say = AsyncMock()

    async def asyncTearDown(self):
        await self.bot.close()
        self.tmp.cleanup()

    def interaction(self, channel=100, user=7):
        return SimpleNamespace(channel=self.channels[channel], channel_id=channel, guild_id=1,
                               user=SimpleNamespace(id=user), response=SimpleNamespace(defer=AsyncMock(),
                               send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))

    def message(self, channel=100, content='fix the tests'):
        return SimpleNamespace(id=900, channel=self.channels[channel], content=content,
                               author=SimpleNamespace(id=7, bot=False), webhook_id=None, attachments=[])

    async def test_harness_change_affects_only_new_threads_and_survives_restart(self):
        project = self.projects.projects['one']
        self.bot.codex.store.sessions[300] = Session(300, project.directory, 'old')
        with patch.dict(upstream.state, {'process': {'thread': 400}}, clear=True):
            await self.bot.set_harness(project, 'claude')
            self.assertEqual(self.bot.backend_for(self.channels[100]), 'claude')
            self.assertEqual(self.bot.backend_for(SimpleNamespace(id=300, parent_id=100)), 'codex')
            self.assertEqual(self.bot.backend_for(SimpleNamespace(id=400, parent_id=100)), 'claude')
            self.assertIsNone(self.bot.backend_for(SimpleNamespace(id=500, parent_id=100)))
        self.assertEqual(ProjectStore(self.projects.path).projects['one'].harness, 'claude')

    async def test_concurrent_projects_keep_separate_contexts(self):
        barrier = asyncio.Event()
        observed = []
        async def route(project):
            with self.bot.project_context(project):
                await barrier.wait()
                observed.append((self.bot.current_project.name, self.bot.codex.main_channel.id))
        tasks = [asyncio.create_task(route(p)) for p in self.projects.projects.values()]
        barrier.set()
        await asyncio.gather(*tasks)
        self.assertEqual(set(observed), {('one', 100), ('two', 200)})
        self.assertIsNone(self.bot.current_project)

    async def test_plain_messages_use_channel_directory_and_default_harness(self):
        self.bot.launch_project_message = AsyncMock()
        for channel, harness in ((100, 'codex'), (200, 'claude')):
            await self.bot.on_message(self.message(channel))
            project, actual, _, prompt = self.bot.launch_project_message.call_args.args
            self.assertEqual((project.channel_id, actual, prompt), (channel, harness, 'fix the tests'))

    async def test_codex_message_keeps_prompt_and_creates_thread_on_source(self):
        project = self.projects.projects['one']
        message = self.message(content='two fix this')
        self.bot.save_attachments = AsyncMock(return_value='')
        self.bot.codex._start_session = AsyncMock()
        await self.bot.launch_project_message(project, 'codex', message, message.content)
        self.bot.codex._start_session.assert_awaited_once_with('two fix this', source_message=message,
                                                             cwd=Path(project.directory))

    async def test_codex_launch_binds_native_cwd_and_discord_parent(self):
        project = self.projects.projects['two']
        thread = SimpleNamespace(id=301, mention='<#301>')
        self.channels[200].create_thread.return_value = thread
        async def call(method, params):
            if method == 'thread/start':
                return {'thread': {'id': 'native', 'cwd': params['cwd']}}
            return {}
        self.bot.codex.live = SimpleNamespace(connect=AsyncMock(), call=AsyncMock(side_effect=call),
                                              subscribed=set(), close=AsyncMock())
        self.bot.codex.say = AsyncMock(return_value=SimpleNamespace(id=900))
        self.bot.codex.send_prompt = AsyncMock()
        with self.bot.project_context(project):
            await self.bot.codex.start_session('fix this')
        self.assertEqual(self.bot.codex.live.call.call_args_list[0].args[1]['cwd'], project.directory)
        self.channels[100].create_thread.assert_not_called()
        self.channels[200].create_thread.assert_awaited_once()
        self.assertEqual(self.bot.codex.store.sessions[301].cwd, project.directory)

    async def test_explicit_harness_launch_uses_same_project_without_changing_default(self):
        self.bot.launch_claude = AsyncMock()
        interaction = self.interaction()
        await self.bot.dispatch_command('claude', None, interaction, {'prompt': 'hello'})
        self.assertEqual(self.bot.launch_claude.call_args.args[0].channel_id, 100)
        self.assertEqual(self.projects.projects['one'].harness, 'codex')

    async def test_attached_claude_adoption_uses_upstream_registration_and_reuses_existing_thread(self):
        project = self.projects.projects['one']
        thread = SimpleNamespace(id=300, send=AsyncMock(return_value=SimpleNamespace(id=500)))
        source = SimpleNamespace(channel=self.channels[100], create_thread=AsyncMock(return_value=thread))
        session = {'key': 'process', 'name': 'work', 'sid': 'native', 'transcript': None,
                   'status': 'idle', 'cwd': project.directory}
        self.bot.get_thread = AsyncMock(return_value=thread)
        with patch.dict(upstream.state, {}, clear=True), patch.object(upstream, 'save_state'), \
             patch.object(upstream, 'status_line', return_value='Ready'):
            result = await self.bot.claude.adopt_attached(session, source)
            self.assertIs(result, thread)
            self.assertEqual(upstream.state['process']['parent'], 100)
            self.assertEqual(upstream.state['process']['thread'], 300)
            self.assertTrue(upstream.state['process']['spawned'])
            self.assertNotIn('type', source.create_thread.call_args.kwargs)
            self.bot.say.assert_not_called()
            self.assertIs(await self.bot.claude.adopt_attached(session, source), thread)
            source.create_thread.assert_awaited_once()

    async def test_failed_attached_claude_thread_releases_upstream_pending_claim(self):
        response = SimpleNamespace(status=403, reason='Forbidden')
        source = SimpleNamespace(channel=self.channels[100], create_thread=AsyncMock(
            side_effect=discord.Forbidden(response, 'Missing permission')))
        session = {'key': 'process', 'name': 'work', 'sid': 'native'}
        with patch.dict(upstream.state, {}, clear=True), patch.object(upstream, 'save_state'), \
             patch.object(upstream, 'log_error'):
            self.assertIsNone(await self.bot.claude.adopt_attached(session, source))
            self.assertNotIn('process', upstream.state)

    async def test_archive_moves_channel_and_blocks_new_prompts_then_unarchives(self):
        project = self.projects.projects['one']
        archived, active = SimpleNamespace(id=8), SimpleNamespace(id=9)
        self.bot.category = AsyncMock(side_effect=[archived, active])
        self.bot.launch_project_message = AsyncMock()
        await self.bot.set_archived(project, True)
        self.assertIs(self.channels[100].edit.call_args.kwargs['category'], archived)
        await self.bot.on_message(self.message())
        self.bot.launch_project_message.assert_not_called()
        self.assertTrue(ProjectStore(self.projects.path).projects['one'].archived)
        await self.bot.set_archived(project, False)
        await self.bot.on_message(self.message())
        self.bot.launch_project_message.assert_awaited_once()

    async def test_failed_discord_edit_does_not_change_saved_project(self):
        project = self.projects.projects['one']
        self.channels[100].edit.side_effect = RuntimeError('network')
        self.bot.category = AsyncMock(return_value=SimpleNamespace(id=8))
        with self.assertRaises(RuntimeError):
            await self.bot.set_archived(project, True)
        self.assertFalse(project.archived)

    async def test_harness_changes_do_not_wait_for_discord_channel_edit_rate_limits(self):
        project = self.projects.projects['one']
        for harness in ('claude', 'codex', 'claude'):
            await self.bot.set_harness(project, harness)
        self.channels[100].edit.assert_not_called()
        self.assertEqual(ProjectStore(self.projects.path).projects['one'].harness, 'claude')
        with patch.object(self.projects, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                await self.bot.set_harness(project, 'codex')
        self.assertEqual(project.harness, 'claude')

    async def test_commands_reject_unallowed_users(self):
        self.bot.create_project = AsyncMock()
        interaction = self.interaction(user=999)
        await self.bot.tree.get_command('project')._do_call(interaction, {'name': 'new', 'dir': '.'})
        self.bot.create_project.assert_not_called()
        interaction.response.send_message.assert_awaited_once()

    async def test_project_commands_schema_and_harness_picker(self):
        for name in ('project', 'harness', 'archive', 'unarchive'):
            self.assertIsNotNone(self.bot.tree.get_command(name))
        self.assertEqual(list(self.bot.tree.get_command('project')._params), ['name', 'dir'])
        self.assertIsNone(self.bot.tree.get_command('hub'))
        interaction = self.interaction()
        await self.bot.tree.get_command('harness')._do_call(interaction, {'name': ''})
        view = interaction.response.send_message.call_args.kwargs['view']
        self.assertEqual([x.value for x in view.select.options], ['codex', 'claude'])
        view.stop()

    async def test_discovery_uses_directory_even_when_default_is_other_harness(self):
        project = self.projects.projects['two']
        self.assertIs(await self.bot.codex.discovery_channel({'cwd': project.directory}), self.channels[200])
        project.archived = True
        self.assertIsNone(await self.bot.codex.discovery_channel({'cwd': project.directory}))
        self.assertIsNone(await self.bot.codex.discovery_channel({'cwd': '/unrelated'}))

    async def test_codex_reply_webhook_belongs_to_actual_thread_parent(self):
        project = self.projects.projects['two']
        thread = SimpleNamespace(id=300, parent=self.channels[200], parent_id=200)
        self.bot.codex.store.sessions[300] = Session(300, project.directory, 'session')
        self.bot.post_as = AsyncMock()
        await self.bot.codex.say(thread, 'reply')
        self.assertIs(self.bot.post_as.call_args.args[0], self.channels[200])

    async def test_create_project_persists_channel_without_harness_channels(self):
        directory = self.root / 'new'
        directory.mkdir()
        guild = SimpleNamespace(create_text_channel=AsyncMock(return_value=SimpleNamespace(id=600)))
        category = SimpleNamespace(guild=guild)
        self.bot.category = AsyncMock(return_value=category)
        result = await self.bot.create_project('new', 'new')
        self.assertEqual(result.directory, str(directory))
        self.assertEqual(ProjectStore(self.projects.path).projects['new'].channel_id, 600)
        self.assertEqual(guild.create_text_channel.call_args.args, ('new',))

    async def test_startup_does_not_run_legacy_claude_setup_or_fetch_harness_channels(self):
        self.bot.fetch_guild = AsyncMock(return_value=SimpleNamespace(id=1, owner_id=7))
        self.bot.fetch_channel = AsyncMock(side_effect=lambda cid: self.channels[cid])
        self.bot.codex.start_backend = AsyncMock()
        self.bot.start_hook_server = AsyncMock()
        self.bot.tree.copy_global_to = Mock()
        self.bot.tree.sync = AsyncMock()
        self.bot.poll_loop = AsyncMock()
        self.bot.claude.start = AsyncMock()
        await self.bot.setup_hook()
        await asyncio.sleep(0)
        self.bot.claude.start.assert_not_called()
        self.assertEqual([call.args[0] for call in self.bot.fetch_channel.call_args_list], [100, 200])


class ProvisionProjectTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_setup_is_idempotent_and_reset_removes_every_old_channel(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(setup_discord, 'HERE', Path(tmp)), \
             patch.object(setup_discord, 'ENV', Path(tmp) / '.env'):
            channels = {}
            guild = SimpleNamespace(id=1, name='cheru-land', owner_id=7, default_role=Mock(), me=Mock(),
                                    channels=[], categories=[], get_member=Mock(return_value=Mock()),
                                    get_channel=lambda cid: channels.get(cid))
            async def create_category(name, **kwargs):
                channel = Mock(spec=discord.CategoryChannel)
                channel.id, channel.name, channel.guild, channel.type = len(channels) + 1, name, guild, 'category'
                channel.delete = AsyncMock()
                channel.category_id = None
                channels[channel.id] = channel
                guild.channels.append(channel)
                guild.categories.append(channel)
                return channel
            async def create_channel(name, **kwargs):
                channel = Mock(spec=discord.TextChannel)
                channel.id, channel.name, channel.guild, channel.type = len(channels) + 1, name, guild, 'text'
                channel.category_id = kwargs['category'].id
                channel.delete = AsyncMock()
                channels[channel.id] = channel
                guild.channels.append(channel)
                return channel
            guild.create_category = AsyncMock(side_effect=create_category)
            guild.create_text_channel = AsyncMock(side_effect=create_channel)
            env = {'PROJECT_STATE_FILE': str(Path(tmp) / 'projects.json')}
            await setup_discord.provision_projects(guild, env, initial_projects=[('work', tmp)])
            self.assertEqual([c.name for c in guild.channels], ['projects', 'archived', 'work'])
            await setup_discord.provision_projects(guild, env)
            self.assertEqual(guild.create_text_channel.await_count, 1)
            old = list(guild.channels)
            for channel in old:
                channel.delete.assert_not_called()
            codex = Path(tmp) / 'private/codex-state.json'
            codex.parent.mkdir(exist_ok=True)
            codex.write_text('{"version":1,"meta":{"hub":999},"sessions":[]}')
            await setup_discord.provision_projects(guild, env, reset=True)
            for channel in old:
                channel.delete.assert_awaited_once()
            store = ProjectStore(env['PROJECT_STATE_FILE'])
            self.assertNotEqual(store.projects['work'].channel_id, old[-1].id)
            self.assertTrue(list((Path(tmp) / 'private/channel-resets').glob('*.json')))
            self.assertNotIn('hub', SessionStore(codex).meta)

    async def test_invalid_seed_is_rejected_before_any_deletion(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(setup_discord, 'HERE', Path(tmp)):
            channel = SimpleNamespace(delete=AsyncMock())
            guild = SimpleNamespace(id=1, channels=[channel])
            with self.assertRaises(ValueError):
                await setup_discord.provision_projects(guild, {}, reset=True,
                    initial_projects=[('missing', str(Path(tmp) / 'missing'))])
            channel.delete.assert_not_called()
