import asyncio
from pathlib import Path
import tempfile
import unittest

from aiohttp import web

from codex_backend import Session
from codex_live import LiveCodex, RpcError, discoverable


class LiveTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chert-', dir='/tmp')
        self.socket = Path(self.tmp.name) / 'server.sock'
        self.calls = []
        self.active = None
        self.fail_steer = False
        self.server_socket = None
        self.hanging = asyncio.Event()
        app = web.Application()
        app.router.add_get('/', self.handle)
        self.server = web.AppRunner(app)
        await self.server.setup()
        await web.UnixSite(self.server, str(self.socket)).start()
        self.client = LiveCodex(self.socket, timeout=1)
        await self.client.connect()

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.cleanup()
        self.tmp.cleanup()

    async def handle(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.server_socket = ws
        async for message in ws:
            data = message.json()
            self.calls.append(data)
            method, params = data.get('method'), data.get('params', {})
            if 'id' not in data:
                continue
            result = {}
            if method == 'thread/loaded/list':
                result = {'data': ['second'] if params.get('cursor') else ['first'],
                          'nextCursor': None if params.get('cursor') else 'page2'}
            elif method == 'thread/read':
                result = {'thread': {'id': params['threadId'], 'cwd': '/project', 'source': 'cli',
                                     'status': {'type': 'active' if self.active else 'idle'}}}
            elif method == 'thread/turns/list':
                result = {'data': [{'id': self.active, 'status': 'inProgress'}] if self.active else []}
            elif method == 'turn/steer' and self.fail_steer:
                await ws.send_json({'id': data['id'], 'error': {'message': 'active turn changed'}})
                continue
            elif method == 'turn/start':
                result = {'turn': {'id': 'new-turn', 'status': 'inProgress'}}
            elif method == 'hang':
                self.hanging.set()
                continue
            elif method == 'fail':
                await ws.send_json({'id': data['id'], 'error': {'message': 'test failure'}})
                continue
            await ws.send_json({'id': data['id'], 'result': result})
        return ws

    async def test_lists_all_pages_and_attaches_without_overriding_configuration(self):
        threads = await self.client.loaded_threads()
        self.assertEqual([t['id'] for t in threads], ['first', 'second'])
        await asyncio.gather(self.client.attach('first'), self.client.attach('first'))
        calls = [c for c in self.calls if c.get('method') == 'thread/resume']
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['params'], {'threadId': 'first', 'excludeTurns': True})

    async def test_progress_notifications_are_forwarded_but_raw_reasoning_is_not(self):
        for method in ('item/agentMessage/delta', 'item/reasoning/summaryTextDelta', 'turn/plan/updated'):
            await self.server_socket.send_json({'method': method, 'params': {'threadId': 'first'}})
            event = await asyncio.wait_for(self.client.notifications.get(), 1)
            self.assertEqual(event['method'], method)
        await self.server_socket.send_json({'method': 'item/reasoning/textDelta', 'params': {'delta': 'private'}})
        await self.client.call('ping', {})
        self.assertTrue(self.client.notifications.empty())

    async def test_idle_message_starts_turn_with_model_but_preserves_permissions(self):
        session = Session(1, '/project', 'test', 'first', model='example-model', effort='high')
        self.assertEqual(await self.client.submit(session, 'hello'), 'started')
        call = next(c for c in self.calls if c.get('method') == 'turn/start')
        self.assertEqual(call['params'], {'threadId': 'first', 'input': [{'type': 'text', 'text': 'hello'}],
                                          'model': 'example-model', 'effort': 'high'})

    async def test_busy_message_steers_same_turn_without_second_writer(self):
        self.active = 'turn-123'
        session = Session(1, '/project', 'test', 'first')
        self.assertEqual(await self.client.submit(session, 'follow-up'), 'steered')
        call = next(c for c in self.calls if c.get('method') == 'turn/steer')
        self.assertEqual(call['params']['expectedTurnId'], 'turn-123')
        self.assertFalse(any(c.get('method') == 'turn/start' for c in self.calls))
        await self.client.interrupt('first')
        self.assertEqual(self.calls[-1]['params'], {'threadId': 'first', 'turnId': 'turn-123'})

    async def test_steer_race_is_reported_without_resending_prompt(self):
        self.active, self.fail_steer = 'turn-123', True
        with self.assertRaises(RpcError):
            await self.client.submit(Session(1, '/project', 'test', 'first'), 'follow-up')
        self.assertEqual(sum(c.get('method') == 'turn/steer' for c in self.calls), 1)
        self.assertFalse(any(c.get('method') == 'turn/start' for c in self.calls))

    async def test_immediate_followup_uses_accepted_turn_even_when_read_status_lags(self):
        session = Session(1, '/project', 'test', 'first')
        await self.client.submit(session, 'first prompt')
        # Fake thread/read still says idle, just as a real daemon can immediately
        # after accepting turn/start. The returned turn ID is authoritative.
        self.assertIsNone(self.active)
        self.assertEqual(await self.client.submit(session, 'follow-up'), 'steered')
        self.assertEqual(sum(c.get('method') == 'turn/start' for c in self.calls), 1)
        self.assertEqual(self.calls[-1]['params']['expectedTurnId'], 'new-turn')

    async def test_notifications_dont_block_rpc_and_do_not_answer_other_clients_approvals(self):
        await self.server_socket.send_json({'method': 'item/completed', 'params': {'threadId': 'first'}})
        await self.server_socket.send_json({'id': 1000, 'method': 'item/commandExecution/requestApproval',
                                           'params': {'threadId': 'first'}})
        first = await asyncio.wait_for(self.client.notifications.get(), 1)
        second = await asyncio.wait_for(self.client.notifications.get(), 1)
        self.assertEqual(first['method'], 'item/completed')
        self.assertEqual(second['method'], 'chert/inputRequired')
        await self.client.call('thread/read', {'threadId': 'first'})
        self.assertFalse(any(c.get('id') == 1000 for c in self.calls))

    async def test_disconnect_fails_pending_rpc_and_reconnect_resubscribes(self):
        await self.client.attach('first')
        task = asyncio.create_task(self.client.call('hang', {}))
        await self.hanging.wait()
        await self.server_socket.close()
        with self.assertRaises(ConnectionError):
            await task
        await self.client.connect()
        await self.client.attach('first')
        self.assertEqual(sum(c.get('method') == 'thread/resume' for c in self.calls), 2)

    async def test_rpc_errors_propagate(self):
        with self.assertRaisesRegex(RpcError, 'test failure'):
            await self.client.call('fail', {})


class DiscoveryScopeTests(unittest.TestCase):
    def test_only_user_facing_loaded_sessions_are_discovered(self):
        root = {'id': 'session', 'cwd': '/some-project', 'source': 'vscode'}
        self.assertTrue(discoverable(root))
        self.assertFalse(discoverable({**root, 'ephemeral': True}))
        self.assertFalse(discoverable({**root, 'source': {'subAgent': {'parent': 'session'}}}))
        self.assertFalse(discoverable({**root, 'canAcceptDirectInput': False}))
