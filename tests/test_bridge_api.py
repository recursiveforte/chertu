from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from aiohttp.test_utils import TestClient, TestServer
from chert.config import Config, CodexOptions
from chert.backends.codex.state import Session, SessionStore
from support import make_frontend


class BridgeAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config = Config("unused", 1, 7, set(), root, root / "state.json")
        self.frontend = make_frontend(config, CodexOptions(), SessionStore(config.state_file))
        self.frontend.hook_secret = Mock(return_value="private-test-secret")
        self.backend = self.frontend.codex
        self.session = Session(300, str(root), "test", "native", backend="app-server")
        self.backend.store.sessions[300] = self.session
        self.backend.live_channel = AsyncMock(return_value=SimpleNamespace(id=300))
        self.backend.send_prompt = AsyncMock()
        self.backend.stop = AsyncMock()
        self.client = TestClient(TestServer(self.frontend.api.create_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.frontend.close()
        self.tmp.cleanup()

    async def test_unauthenticated_requests_cannot_control_sessions(self):
        for route in ("send", "screen"):
            response = await self.client.post(
                "/codex/" + route, json={"sid": "native", "text": "hello"}
            )
            self.assertEqual(response.status, 403)
        self.backend.send_prompt.assert_not_called()
        self.backend.live_channel.assert_not_called()

    async def test_authenticated_send_and_escape_use_native_controller(self):
        headers = {"X-Hearth-Secret": "private-test-secret"}
        response = await self.client.post(
            "/codex/send", headers=headers, json={"sid": "native", "text": "hello"}
        )
        self.assertEqual(response.status, 200)
        self.backend.send_prompt.assert_awaited_once()
        self.assertEqual(self.backend.send_prompt.call_args.args[1], "hello")
        response = await self.client.post(
            "/codex/send", headers=headers, json={"sid": "native", "key": "Escape"}
        )
        self.assertEqual(response.status, 200)
        self.backend.stop.assert_awaited_once()

    async def test_missing_and_ended_sessions_keep_their_http_errors(self):
        headers = {"X-Hearth-Secret": "private-test-secret"}
        response = await self.client.post("/codex/send", headers=headers, json={"sid": "missing"})
        self.assertEqual(response.status, 404)
        self.session.status = "ended"
        response = await self.client.post("/codex/send", headers=headers, json={"sid": "native"})
        self.assertEqual(response.status, 409)
        self.backend.send_prompt.assert_not_called()
