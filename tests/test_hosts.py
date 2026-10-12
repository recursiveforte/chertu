import asyncio
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web

from chert.config import Config, CodexOptions
from chert.hosts import Host, RemoteHost, load_hosts
from chert.host_worker import execute
from chert.projects import Project, ProjectStore
from chert.backends.codex.state import Session, SessionStore, HostSessionStore
from chert.backends.codex.client import LiveCodex
from chert.web import dashboard
from support import make_frontend

SID = "11111111-1111-4111-8111-111111111111"


class HostStateTests(unittest.TestCase):
    def test_legacy_and_remote_sessions_save_together_without_id_collisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps({"version": 1, "sessions": [
                {"discord_thread": 100, "cwd": tmp, "name": "old", "codex_thread": SID}
            ]}))
            store = SessionStore(path)
            local, remote = HostSessionStore(store, "local"), HostSessionStore(store, "macbook")
            remote.sessions[200] = Session(200, "/Users/test/work", "remote", SID)
            remote.meta["model"] = "remote-model"
            remote.save()
            self.assertEqual(list(local.sessions), [100])
            self.assertEqual(list(remote.sessions), [200])
            with self.assertRaises(KeyError):
                del local.sessions[200]
            with self.assertRaises(ValueError):
                local.sessions[200] = Session(200, tmp, "wrong")
            self.assertNotIn("model", local.meta)
            loaded = SessionStore(path)
            self.assertEqual(loaded.sessions[100].host, "local")
            self.assertEqual(loaded.sessions[200].host, "macbook")
            self.assertNotEqual(local.path.parent, remote.path.parent)

    def test_same_paths_on_different_hosts_are_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ProjectStore(Path(tmp) / "projects.json")
            store.projects["linux"] = Project("linux", tmp, 100)
            store.projects["mac"] = Project("mac", tmp, 200, host="macbook")
            self.assertEqual(store.for_directory(tmp).name, "linux")
            self.assertEqual(store.for_directory(tmp, "macbook").name, "mac")
            self.assertIsNone(store.for_directory(tmp, "unconfigured"))
            store.save()
            self.assertEqual(ProjectStore(store.path).projects["mac"].host, "macbook")

    def test_remote_paths_are_not_resolved_against_gateway_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "link"
            link.symlink_to("/tmp")
            project = Project("remote", str(link), 200, host="macbook")
            self.assertEqual(project.directory, str(link))

    def test_host_config_rejects_options_duplicate_names_and_unsafe_paths(self):
        for values in (
            dict(name="../escape", ssh="mac", socket="/tmp/a"),
            dict(name="mac", ssh="-oProxyCommand=bad", socket="/tmp/a"),
            dict(name="mac", ssh="mac -v", socket="/tmp/a"),
            dict(name="mac", ssh="mac", socket="~/codex.sock"),
            dict(name="mac", ssh="mac", socket="/tmp/a:b"),
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Host(**values)
        with tempfile.TemporaryDirectory() as tmp:
            config = Config("test", 1, 7, set(), Path(tmp), Path(tmp) / "state.json")
            path = Path(tmp) / "hosts.json"
            path.write_text('{"hosts": [{"name": "local"}]}')
            with self.assertRaises(ValueError):
                load_hosts(path, config)

    def test_dashboard_journals_and_identical_native_ids_are_host_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(json.dumps({"sessions": [
                {"codex_thread": SID, "cwd": tmp, "name": name, "status": "idle", "host": host}
                for name, host in (("local", "local"), ("remote", "macbook"))
            ]}))
            with patch.object(dashboard, "STATE", state):
                self.assertEqual(dashboard.find(SID)["name"], "local")
                self.assertEqual(dashboard.find("macbook~" + SID)["name"], "remote")
                for sid, text in ((SID, "local reply"), ("macbook~" + SID, "remote reply")):
                    path = dashboard.transcript_path(sid)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({"type": "assistant", "message": {
                        "content": [{"type": "text", "text": text}]
                    }}) + "\n")
                    response = dashboard.app.test_client().get("/codex/api/transcript/" + sid)
                    self.assertIn(text, response.json["items"][0]["html"])
                with self.assertRaises(ValueError):
                    dashboard.transcript_path("../escape~" + SID)


class HostRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        path = root / "hosts.json"
        path.write_text(json.dumps({"hosts": [{"name": "macbook", "ssh": "macbook",
                                              "socket": "/Users/test/.codex/control.sock"}]}))
        config = Config("test", 1, 7, set(), root, root / "state.json", hosts_file=path)
        self.bot = make_frontend(config, CodexOptions(), SessionStore(config.state_file))
        self.project = Project("mac", "/Users/test/work", 500, host="macbook")
        self.bot.projects.projects["mac"] = self.project
        self.channel = SimpleNamespace(id=500, parent_id=None)
        self.thread = SimpleNamespace(id=600, parent_id=500)
        self.bot.project_channels[500] = self.channel
        self.remote = self.bot.codex_hosts["macbook"]
        self.remote.owner = 7
        self.remote.store.sessions[600] = Session(600, self.project.directory, "remote", SID,
                                                  project_channel=500)
        self.bot.codex.store.sessions[300] = Session(300, self.tmp.name, "local", SID)
        self.bot._connection.user = SimpleNamespace(id=42)

    async def asyncTearDown(self):
        await self.bot.close()
        self.tmp.cleanup()

    def interaction(self):
        return SimpleNamespace(channel=self.thread, channel_id=600, guild_id=1,
                               user=SimpleNamespace(id=7),
                               response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                               followup=SimpleNamespace(send=AsyncMock()))

    async def test_session_host_overrides_project_default_and_missing_host_never_falls_back(self):
        self.project.host = "local"
        self.assertIs(self.bot.codex_for(self.thread), self.remote)
        self.assertIs(self.bot.codex_for(self.channel), self.bot.codex)
        del self.bot.codex_hosts["macbook"]
        self.assertEqual(self.bot.backend_for(self.thread), "codex")
        with self.assertRaisesRegex(ValueError, "not configured"):
            self.bot.codex_for(self.thread)
        self.bot.codex_hosts["macbook"] = self.remote

    async def test_commands_reactions_and_model_picker_use_remote_connection(self):
        self.bot.codex.controls.execute = AsyncMock()
        self.remote.controls.execute = AsyncMock()
        interaction = self.interaction()
        await self.bot.dispatch_command("effort", None, interaction, {"level": "high"})
        self.remote.controls.execute.assert_awaited_once()
        self.bot.codex.controls.execute.assert_not_called()
        self.bot.codex.reactions.click = AsyncMock()
        self.remote.reactions.click = AsyncMock()
        await self.bot.on_raw_reaction_add(SimpleNamespace(channel_id=600))
        self.remote.reactions.click.assert_awaited_once()
        self.bot.codex.reactions.click.assert_not_called()
        self.remote.live.connect = AsyncMock()
        self.remote.live.call = AsyncMock(side_effect=[
            {"data": [{"model": "mac-model"}]}, {"thread": {"model": "mac-model"}},
        ])
        self.bot.codex.live.connect = AsyncMock()
        await self.bot.dispatch_command("model", None, self.interaction(), {"name": ""})
        self.remote.live.connect.assert_awaited_once()
        self.bot.codex.live.connect.assert_not_called()

    async def test_offline_remote_prompt_reports_failure_without_local_launch(self):
        self.remote.remote.run = AsyncMock(side_effect=ConnectionError("MacBook offline"))
        self.bot.codex.start_session = AsyncMock()
        self.bot.say = AsyncMock()
        message = SimpleNamespace(content="hello", channel=self.channel, attachments=[],
                                  author=SimpleNamespace(id=7), webhook_id=None)
        await self.bot.route_message(message)
        self.assertIn("MacBook offline", self.bot.say.call_args.args[1])
        self.bot.codex.start_session.assert_not_called()

    async def test_remote_worktree_launch_and_upload_use_remote_paths(self):
        self.remote.remote.run = AsyncMock(side_effect=["/remote/worktree", "/remote/upload"])
        self.remote.start_session = AsyncMock(return_value=SimpleNamespace(mention="<#600>"))
        with patch("chert.backends.codex.backend.session_directory") as local:
            await self.remote.launch(self.project, "hello", SimpleNamespace(id=7), AsyncMock(), worktree=True)
        local.assert_not_called()
        self.assertEqual(self.remote.start_session.call_args.kwargs["cwd"], Path("/remote/worktree"))
        attachment = SimpleNamespace(size=5, filename="a.png", read=AsyncMock(return_value=b"image"))
        result = await self.remote.save_attachments(SimpleNamespace(attachments=[attachment]))
        self.assertIn("/remote/upload", result)
        self.assertEqual(self.remote.remote.run.call_args.args, ("upload",))

    async def test_remote_host_commands_and_claude_cannot_touch_local_host(self):
        self.bot.handle_main_command = AsyncMock()
        for command in ("disk", "backup", "s3", "offload", "restore", "feldspar"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                await self.remote.controls.execute(command, self.thread, SimpleNamespace(id=7), {}, AsyncMock())
        self.bot.handle_main_command.assert_not_called()
        with self.assertRaises(ValueError):
            self.bot.harness_for("claude", project=self.project)

    async def test_remote_discovery_cannot_mark_local_session_disconnected(self):
        self.remote.live.loaded_threads = AsyncMock(return_value=[])
        self.remote.events.update_thread_title = AsyncMock()
        self.remote.events.update_live_status = AsyncMock()
        self.remote.store.sessions[600].backend = "app-server"
        await self.remote.discovery.scan()
        self.assertEqual(self.remote.store.sessions[600].status, "disconnected")
        self.assertEqual(self.bot.codex.store.sessions[300].status, "idle")

    async def test_bridge_api_targets_host_qualified_session_id(self):
        self.bot.hook_secret = Mock(return_value="test")
        self.remote.live_channel = AsyncMock(return_value=self.thread)
        self.remote.send_prompt = AsyncMock()
        self.bot.codex.send_prompt = AsyncMock()
        request = SimpleNamespace(headers={"X-Hearth-Secret": "test"},
                                  json=AsyncMock(return_value={"sid": "macbook~" + SID, "text": "hello"}))
        result = await self.bot.api.codex_send_http(request)
        self.assertEqual(result.status, 200)
        self.remote.send_prompt.assert_awaited_once_with(self.thread, "hello")
        self.bot.codex.send_prompt.assert_not_called()


class WorkerTests(unittest.TestCase):
    def test_helper_runs_standalone_without_chert_or_discord_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(__file__).parents[1] / "chert/host_worker.py"
            proc = subprocess.run([sys.executable, "-I", "-S", str(source)], input=json.dumps({
                "operation": "directory", "params": {"directory": "."}, "root": tmp, "runtime": tmp,
            }), text=True, capture_output=True, check=True)
            self.assertEqual(json.loads(proc.stdout), {"result": tmp})

    def test_upload_and_download_are_private_and_preserve_binary_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = {"root": tmp, "runtime": tmp, "operation": "upload", "params": {
                "name": "../../a;$(echo bad).png", "data": base64.b64encode(b"\x00image\xff").decode(),
            }}
            path = Path(execute(request))
            self.assertEqual(path.parent, Path(tmp) / "attachments")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            result = execute({**request, "operation": "download", "params": {"path": str(path), "cwd": tmp}})
            self.assertEqual(base64.b64decode(result["data"]), b"\x00image\xff")

    def test_worktree_runs_in_target_repository_and_preserves_dirty_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            def git(*args):
                return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
            git("init")
            git("config", "user.email", "test@example.invalid")
            git("config", "user.name", "Test")
            (root / "file").write_text("committed")
            git("add", "file")
            git("commit", "-m", "initial")
            (root / "file").write_text("dirty")
            dest = Path(execute({"operation": "worktree", "root": str(root), "runtime": tmp,
                                 "params": {"directory": ".", "channel_id": 500}}))
            self.assertEqual((dest / "file").read_text(), "committed")
            self.assertEqual((root / "file").read_text(), "dirty")
            self.assertTrue(dest.is_relative_to(Path(tmp) / "worktrees" / "500"))


class HostTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_connections_isolate_events_approvals_and_disconnects(self):
        with tempfile.TemporaryDirectory() as tmp:
            runners, clients, sockets = [], [], {}
            try:
                for host in ("linux", "mac"):
                    async def handle(request, name=host):
                        ws = web.WebSocketResponse()
                        await ws.prepare(request)
                        sockets[name] = ws
                        async for message in ws:
                            data = message.json()
                            if "id" in data and "method" in data:
                                await ws.send_json({"id": data["id"], "result": {"host": name}})
                        return ws
                    app = web.Application()
                    app.router.add_get("/", handle)
                    runner = web.AppRunner(app)
                    runners.append(runner)
                    await runner.setup()
                    path = Path(tmp) / (host + ".sock")
                    await web.UnixSite(runner, str(path)).start()
                    remote = SimpleNamespace(connect=AsyncMock(return_value=path))
                    client = LiveCodex(Path("/unused-remote-path"), remote=remote)
                    clients.append(client)
                    await client.connect()
                    self.assertEqual((await client.call("ping", {}))["host"], host)
                for name in sockets:
                    await sockets[name].send_json({"id": 7, "method": "item/tool/requestUserInput",
                                                   "params": {"threadId": SID, "questions": []}})
                prompts = [await asyncio.wait_for(client.notifications.get(), 1) for client in clients]
                self.assertNotEqual(prompts[0]["requestKey"], prompts[1]["requestKey"])
                self.assertNotIn(prompts[1]["requestKey"], clients[0].server_requests)
                await clients[1].close()
                self.assertEqual((await clients[0].call("ping", {}))["host"], "linux")
            finally:
                for client in clients:
                    await client.close()
                for runner in runners:
                    await runner.cleanup()

    async def test_remote_helper_protocol_and_shell_quoting(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Host("test", ssh="test", socket="/tmp/socket", python=sys.executable,
                        runtime=tmp, project_root=tmp)
            remote = RemoteHost(host)
            # Stand in for SSH's remote shell, exercising the exact helper command
            # and JSON stdin without contacting another machine.
            command = [sys.executable, "-c",
                       "import os,sys; os.execl('/bin/sh', 'sh', '-c', sys.argv[-1])"]
            with patch.object(Host, "ssh_command", return_value=command):
                self.assertEqual(await remote.run("directory", directory="."), tmp)
                with self.assertRaisesRegex(ValueError, "does not exist"):
                    await remote.run("directory", directory="$(touch should-not-exist)")
                self.assertFalse((Path(tmp) / "should-not-exist").exists())
