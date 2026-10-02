"""Connect to the existing Codex daemon; never spawn a second session writer.

The daemon speaks app-server JSON-RPC over a local Unix WebSocket. Discovery only
lists loaded threads, rather than replaying the user's entire stored history.
"""
import asyncio
from pathlib import Path

import aiohttp


class RpcError(RuntimeError):
    pass


class LiveCodex:
    def __init__(self, socket: Path, timeout=30):
        self.socket, self.timeout = Path(socket), timeout
        self.http = self.ws = self.reader = None
        self.pending = {}
        self.sequence = 0
        self.notifications = asyncio.Queue(maxsize=2048)
        self.subscribed = set()
        self.active_turns = {}
        self.completed_turns = set()
        self.connect_lock = asyncio.Lock()
        self.attach_lock = asyncio.Lock()

    @property
    def connected(self):
        return self.ws is not None and not self.ws.closed and self.reader is not None and not self.reader.done()

    async def connect(self):
        async with self.connect_lock:
            await self._connect()

    async def _connect(self):
        if self.connected:
            return
        await self.close()
        self.http = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(self.socket)))
        try:
            self.ws = await self.http.ws_connect('http://localhost', max_msg_size=16 * 1024 * 1024)
            self.reader = asyncio.create_task(self._read())
            await self.call('initialize', {
                'clientInfo': {'name': 'chert', 'title': 'Chert Discord bridge', 'version': '0.2.0'},
                'capabilities': {'experimentalApi': True},
            })
            await self.ws.send_json({'method': 'initialized', 'params': {}})
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.reader is not None:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        if self.ws is not None:
            await self.ws.close()
        if self.http is not None:
            await self.http.close()
        self.reader = self.ws = self.http = None
        self.subscribed.clear()
        self.active_turns.clear()
        self.completed_turns.clear()
        self._fail_pending()

    def _fail_pending(self):
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ConnectionError('Codex app-server disconnected.'))

    async def _read(self):
        try:
            async for message in self.ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = message.json()
                if data.get('method') in {'turn/started', 'turn/completed'}:
                    params = data.get('params') or {}
                    thread_id, turn_id = params.get('threadId'), (params.get('turn') or {}).get('id')
                    if thread_id and turn_id:
                        if data['method'] == 'turn/started':
                            self.active_turns[thread_id] = turn_id
                        else:
                            if self.active_turns.get(thread_id) == turn_id:
                                self.active_turns.pop(thread_id, None)
                            if len(self.completed_turns) > 1024:
                                self.completed_turns.clear()
                            self.completed_turns.add(turn_id)
                if 'method' in data:
                    if 'id' in data:
                        # Another client owns these approvals. Don't answer (or deny)
                        # them on its behalf; tell the Discord user where to respond.
                        self.notifications.put_nowait({
                            'method': 'chert/inputRequired', 'params': data.get('params') or {}})
                    elif data['method'] in {
                        'item/completed', 'turn/started', 'turn/completed',
                        'thread/status/changed', 'thread/name/updated',
                    }:
                        # Never block RPC responses behind slow Discord sends. A full
                        # queue causes a reconnect, rather than deadlocking RPC calls.
                        self.notifications.put_nowait(data)
                elif data.get('id') in self.pending:
                    future = self.pending[data['id']]
                    if not future.done():
                        if 'error' in data:
                            future.set_exception(RpcError(data['error'].get('message', str(data['error']))))
                        else:
                            future.set_result(data.get('result', {}))
        finally:
            self._fail_pending()

    async def call(self, method, params):
        if self.ws is None or self.ws.closed:
            raise ConnectionError('Codex app-server is not connected.')
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.ws.send_json({'id': request_id, 'method': method, 'params': params})
            return await asyncio.wait_for(future, self.timeout)
        finally:
            self.pending.pop(request_id, None)

    async def loaded_threads(self):
        result, cursor = [], None
        while True:
            page = await self.call('thread/loaded/list', {'cursor': cursor, 'limit': 100})
            for thread_id in page['data']:
                try:
                    response = await self.call('thread/read', {'threadId': thread_id, 'includeTurns': False})
                    result.append(response['thread'])
                except RpcError:
                    pass  # A thread may unload between the list and read.
            cursor = page.get('nextCursor')
            if not cursor:
                return result

    async def attach(self, thread_id):
        async with self.attach_lock:
            if thread_id not in self.subscribed:
                # No configuration overrides: preserve the original client's settings.
                # Excluding turns avoids hydrating potentially huge historical transcripts.
                await self.call('thread/resume', {'threadId': thread_id, 'excludeTurns': True})
                self.subscribed.add(thread_id)

    async def active_turn(self, thread_id):
        if thread_id in self.active_turns:
            return self.active_turns[thread_id]
        info = await self.call('thread/read', {'threadId': thread_id, 'includeTurns': False})
        if (info['thread'].get('status') or {}).get('type') != 'active':
            return None
        result = await self.call('thread/turns/list', {
            'threadId': thread_id, 'limit': 1, 'sortDirection': 'desc', 'itemsView': 'notLoaded',
        })
        return next((t['id'] for t in result['data'] if t['status'] == 'inProgress'), None)

    async def submit(self, session, prompt):
        await self.connect()
        await self.attach(session.codex_thread)
        inputs = [{'type': 'text', 'text': prompt}]
        turn_id = await self.active_turn(session.codex_thread)
        if turn_id:
            await self.call('turn/steer', {'threadId': session.codex_thread,
                                         'expectedTurnId': turn_id, 'input': inputs})
            return 'steered'
        params = {'threadId': session.codex_thread, 'input': inputs}
        if session.model:
            params['model'] = session.model
        if session.effort:
            params['effort'] = session.effort
        result = await self.call('turn/start', params)
        turn = result.get('turn') or {}
        if turn.get('id') and turn.get('status') == 'inProgress' and turn['id'] not in self.completed_turns:
            # The runtime status in thread/read can lag this response. Track the
            # accepted turn immediately so a fast follow-up steers that exact turn.
            self.active_turns[session.codex_thread] = turn['id']
        return 'started'

    async def interrupt(self, thread_id):
        await self.connect()
        turn_id = await self.active_turn(thread_id)
        if turn_id:
            await self.call('turn/interrupt', {'threadId': thread_id, 'turnId': turn_id})


def discoverable(thread):
    """Discover user-facing sessions, not internal subagents or ephemeral jobs."""
    source = thread.get('source')
    return bool(thread.get('id') and thread.get('cwd') and not thread.get('ephemeral')
                and not isinstance(source, dict)  # subAgent source
                and thread.get('canAcceptDirectInput') is not False)


def live_status(thread):
    status = (thread.get('status') or {}).get('type')
    return {'active': 'running', 'idle': 'idle', 'systemError': 'error'}.get(status, 'disconnected')
