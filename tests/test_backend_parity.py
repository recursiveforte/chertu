import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import discord

from codex_backend import CodexRunner, Session, SessionStore
from codex_bot import Config
from shared_frontend import SharedFrontend
from shared_prompts import request_view
from backends.codex import text_arguments

SID = '11111111-1111-4111-8111-111111111111'
FORK = '22222222-2222-4222-8222-222222222222'


class BackendParityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        cfg = Config('unused', 100, 7, set(), root, root/'state.json')
        self.host = SharedFrontend(cfg, CodexRunner(), SessionStore(cfg.state_file), 200)
        self.host.owner = self.host.codex.owner = 7
        self.host._connection.user = SimpleNamespace(id=999)
        self.adapter = self.host.codex
        self.channel = SimpleNamespace(id=300, parent_id=100, archived=False, mention='<#300>',
            edit=AsyncMock(), delete=AsyncMock(), send=AsyncMock(return_value=SimpleNamespace(id=900)),
            get_partial_message=Mock(return_value=SimpleNamespace(edit=AsyncMock())))
        self.adapter.main_channel = SimpleNamespace(id=100, create_thread=AsyncMock(return_value=self.channel))
        self.adapter.fetch_channel = AsyncMock(return_value=self.channel)
        self.adapter.say = AsyncMock(return_value=SimpleNamespace(id=900))
        self.adapter.webhook_for = AsyncMock(return_value=SimpleNamespace(edit_message=AsyncMock()))
        self.session = Session(300, str(root), 'original', SID, backend='app-server', display_model='test-model')
        self.adapter.store.sessions[300] = self.session
        async def call(method, params):
            if method == 'thread/fork':
                return {'thread': {'id': FORK, 'cwd': str(root), 'name': 'original-fork'}}
            if method in {'thread/read', 'thread/resume'}:
                return {'thread': {'id': params['threadId'], 'cwd': str(root), 'name': 'original-fork', 'status': {'type': 'idle'}}}
            if method in {'thread/list', 'thread/turns/list'}:
                return {'data': []}
            return {}
        self.adapter.live = SimpleNamespace(connect=AsyncMock(), attach=AsyncMock(), close=AsyncMock(),
            call=AsyncMock(side_effect=call), interrupt=AsyncMock(), active_turn=AsyncMock(return_value=None),
            submit=AsyncMock(return_value='started'), subscribed=set(), server_requests={}, answer=AsyncMock())
        self.user = SimpleNamespace(id=7, display_name='owner', bot=False)
        self.respond = AsyncMock()

    async def asyncTearDown(self):
        await self.host.close()
        self.tmp.cleanup()

    async def execute(self, name, **kwargs):
        await self.adapter.execute(name, self.channel, self.user, kwargs, self.respond)

    async def test_restart_unloads_only_selected_actor_and_preserves_history_id(self):
        await self.execute('restart', force=False)
        methods = [c.args[0] for c in self.adapter.live.call.call_args_list]
        self.assertEqual(methods[:2], ['thread/archive', 'thread/unarchive'])
        self.assertEqual(self.session.codex_thread, SID)
        self.assertEqual(self.session.discord_thread, 300)

    async def test_busy_restart_requires_force(self):
        self.session.status = 'running'
        await self.execute('restart', force=False)
        self.adapter.live.call.assert_not_called()
        self.assertIn('busy', self.respond.call_args.args[0])

    async def test_kill_ends_native_thread_but_does_not_delete_history(self):
        await self.execute('kill', how='hard')
        self.adapter.live.call.assert_awaited_with('thread/archive', {'threadId': SID})
        self.assertEqual(self.session.status, 'ended')
        self.channel.delete.assert_not_called()

    async def test_fork_uses_native_history_copy_and_upstream_name(self):
        self.adapter.start_session = AsyncMock(return_value=self.channel)
        await self.execute('fork', to='same', message='')
        self.adapter.live.call.assert_any_await('thread/fork', {'threadId': SID, 'excludeTurns': True})
        self.adapter.live.call.assert_any_await('thread/name/set', {'threadId': FORK, 'name': 'original-fork'})
        self.adapter.start_session.assert_awaited_once_with('', codex_id=FORK)

    async def test_cross_backend_fork_does_not_launch_unsigned_claude(self):
        self.host.claude_enabled = False
        await self.execute('fork', to='claude', message='continue')
        self.adapter.live.call.assert_not_called()
        self.assertIn('signed in', self.respond.call_args.args[0])

    async def test_plan_mode_and_default_are_real_backend_settings(self):
        await self.execute('mode', mode='plan')
        self.assertEqual(self.session.collaboration_mode, 'plan')
        await self.execute('mode', mode='default')
        self.assertEqual(self.session.collaboration_mode, 'default')
        self.adapter.live.call.assert_awaited_with('thread/resume', {
            'threadId': SID, 'excludeTurns': True, 'approvalPolicy': 'on-request',
            'sandbox': 'workspace-write', 'approvalsReviewer': 'user'})

    async def test_mute_suppresses_model_output_but_retains_history(self):
        await self.execute('mute')
        self.adapter.say.reset_mock()
        await self.adapter.handle_live_event({'method': 'item/completed', 'params': {
            'threadId': SID, 'turnId': 'turn', 'item': {'id': 'item', 'type': 'agentMessage', 'text': 'saved answer'}}})
        self.adapter.say.assert_not_called()
        self.assertEqual(self.session.recent[-1]['text'], 'saved answer')
        self.assertTrue((self.adapter.store.path.parent/'codex-transcripts'/f'{SID}.jsonl').exists())

    async def test_supernova_is_persistent_and_cancellable(self):
        await self.execute('supernova', minutes=22, then='wrap')
        saved = SessionStore(self.adapter.store.path).sessions[300]
        self.assertEqual(saved.deadline['action'], 'wrap')
        self.assertEqual(saved.deadline['message'], 900)
        await self.execute('supernova', cancel=True)
        self.assertIsNone(self.session.deadline)
        self.channel.get_partial_message.assert_called_with(900)

    async def test_attachment_import_uses_upstream_path_convention(self):
        self.host.save_attachments = AsyncMock(return_value='[attachment: /uploads/image.png]')
        self.adapter.send_prompt = AsyncMock(return_value='🤔')
        message = SimpleNamespace(id=10, author=self.user, channel=self.channel, content='look',
            attachments=[object()], webhook_id=None, mentions=[], add_reaction=AsyncMock())
        await self.adapter.on_message(message)
        self.host.save_attachments.assert_awaited_once_with(message)
        self.adapter.send_prompt.assert_awaited_once_with(self.channel, 'look\n[attachment: /uploads/image.png]')

    async def test_approval_is_sent_only_after_explicit_click(self):
        key = 'connection:1'
        request = {'id': 1, 'method': 'item/commandExecution/requestApproval', 'params': {'command': 'do something'}}
        self.adapter.live.server_requests[key] = request
        body, view = request_view(self.adapter, key, request)
        self.adapter.live.answer.assert_not_called()
        interaction = SimpleNamespace(user=self.user, channel=self.channel,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()), message=SimpleNamespace(edit=AsyncMock()))
        self.assertTrue(await view.interaction_check(interaction))
        await view.children[0].callback(interaction)
        self.adapter.live.answer.assert_awaited_once_with(key, {'decision': 'accept'})

    async def test_expired_approval_cannot_affect_a_new_connection(self):
        body, view = request_view(self.adapter, 'old-connection:1', {'id': 1,
            'method': 'item/fileChange/requestApproval', 'params': {}})
        interaction = SimpleNamespace(user=self.user, channel=self.channel,
            response=SimpleNamespace(send_message=AsyncMock()))
        self.assertFalse(await view.interaction_check(interaction))
        self.adapter.live.answer.assert_not_called()

    async def test_board_pin_failure_does_not_create_duplicate_boards(self):
        denied = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'missing permission')
        message = SimpleNamespace(id=901, pin=AsyncMock(side_effect=denied))
        self.adapter.main_channel.send = AsyncMock(return_value=message)
        self.host.board_for = AsyncMock(return_value=('board', 'board with footer'))
        await self.adapter.board_tick()
        await self.adapter.board_tick()
        self.adapter.main_channel.send.assert_awaited_once()
        self.assertEqual(SessionStore(self.adapter.store.path).meta['board_msg'], 901)

    async def test_reconnect_recovers_a_missed_reply_once_without_rerunning_the_turn(self):
        self.session.mirror_since = 100
        self.session.status = 'running'
        self.session.active_turn = 'current-turn'
        self.adapter.live.call.side_effect = None
        self.adapter.live.call.return_value = {'data': [{'id': 'missed-turn', 'status': 'completed',
            'completedAt': 110, 'items': [{'id': 'answer', 'type': 'agentMessage', 'text': 'Recovered reply'}]}]}
        await self.adapter.catch_up(self.session)
        await self.adapter.catch_up(self.session)
        self.adapter.say.assert_awaited_once_with(self.channel, 'Recovered reply')
        self.adapter.live.submit.assert_not_called()
        self.assertEqual(self.session.status, 'running')
        self.assertEqual(self.session.active_turn, 'current-turn')

    async def test_reconnect_does_not_replay_history_before_the_discord_thread_existed(self):
        self.session.mirror_since = 100
        self.adapter.live.call.side_effect = None
        self.adapter.live.call.return_value = {'data': [{'id': 'historical-turn', 'status': 'completed',
            'completedAt': 50, 'items': [{'id': 'answer', 'type': 'agentMessage', 'text': 'Old history'}]}]}
        await self.adapter.catch_up(self.session)
        self.adapter.say.assert_not_called()

    async def test_native_rename_uses_upstream_background_title_updates(self):
        self.host.retitle = Mock()
        self.host.say = AsyncMock()
        await self.adapter.handle_live_event({'method': 'thread/name/updated', 'params': {
            'threadId': SID, 'threadName': 'new-name'}})
        self.assertEqual(self.session.name, 'new-name')
        self.host.retitle.assert_called_once_with(self.channel, '🚀 new-name')

    def test_upstream_text_aliases_and_fleet_forms(self):
        self.assertEqual(text_arguments('revive', 'all'), {'_command': 'reviveall'})
        self.assertEqual(text_arguments('restart', 'all force'), {'_command': 'restartall', 'force': True})
        self.assertEqual(text_arguments('bypass', ''), {'_command': 'mode', 'mode': 'bypass'})
        self.assertEqual(text_arguments('supernova', '1h stop')['seconds'], 3600)
