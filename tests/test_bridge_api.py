from pathlib import Path
import asyncio
import json
import sys
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

    async def test_project_api_auth_validation_creation_and_duplicates(self):
        channel = SimpleNamespace(id=600)
        guild = SimpleNamespace(create_text_channel=AsyncMock(return_value=channel))
        self.frontend.project_commands.category = AsyncMock(return_value=SimpleNamespace(guild=guild))
        for method in (self.client.get, self.client.post):
            response = await method("/projects")
            self.assertEqual(response.status, 403)
        headers = {"X-Hearth-Secret": "private-test-secret"}
        for body in ([], {}, {"name": 12}, {"name": "../escape"},
                     {"name": "fresh", "directory": ""}, {"name": "fresh", "host": "unknown"},
                     {"name": "fresh", "unexpected": True}):
            response = await self.client.post("/projects", headers=headers, json=body)
            self.assertEqual(response.status, 400, await response.text())
        guild.create_text_channel.assert_not_called()
        response = await self.client.post("/projects", headers=headers, json={"name": "fresh"})
        self.assertEqual(response.status, 201, await response.text())
        result = await response.json()
        self.assertTrue(Path(result["directory"]).is_dir())
        self.assertEqual(result["url"], "https://discord.com/channels/1/600")
        response = await self.client.post("/projects", headers=headers, json={"name": "fresh"})
        self.assertEqual(response.status, 400)
        guild.create_text_channel.assert_awaited_once()
        response = await self.client.get("/projects", headers=headers)
        result = await response.json()
        self.assertIn("fresh", [p["name"] for p in result["projects"]])
        self.assertEqual(result["hosts"][0]["name"], "local")

    async def test_agent_cli_creates_through_real_http_and_keeps_secret_private(self):
        guild = SimpleNamespace(create_text_channel=AsyncMock(return_value=SimpleNamespace(id=601)))
        self.frontend.project_commands.category = AsyncMock(return_value=SimpleNamespace(guild=guild))
        secret_file = Path(self.tmp.name) / "secret"
        secret_file.write_text("private-test-secret")
        helper = Path(__file__).parents[1] / "bin/chert-project"

        async def run(*args):
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(helper), "--port", str(self.client.server.port),
                "--secret-file", str(secret_file), *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            self.assertNotIn(b"private-test-secret", out + err)
            return proc.returncode, out, err

        code, out, err = await run("create", "from-chat")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["channel_id"], 601)
        self.assertTrue((Path(self.tmp.name) / "from-chat").is_dir())
        code, out, err = await run("list")
        self.assertEqual(code, 0, err)
        self.assertIn("from-chat", [p["name"] for p in json.loads(out)["projects"]])
        code, out, err = await run("create", "from-chat")
        self.assertEqual(code, 1)
        self.assertIn(b"already exists", err)
