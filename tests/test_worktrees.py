import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from chert.projects import Project, ProjectStore
from chert.worktrees import session_directory
import test_projects
from chert.vendor import bridge as upstream


def git(directory, *args):
    return subprocess.check_output(
        ["git", "-C", str(directory), *args], text=True, stderr=subprocess.PIPE
    ).strip()


def repository(directory):
    git(directory, "init", "-q")
    (directory / "tracked.txt").write_text("committed")
    (directory / "sub").mkdir()
    (directory / "sub/file.txt").write_text("subproject")
    git(directory, "add", ".")
    git(directory, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "initial")


class WorktreeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        repository(self.repo)
        self.store = ProjectStore(self.root / "private/projects.json")
        self.project = Project("work", str(self.repo), 123, worktrees=True)
        self.store.projects["work"] = self.project
        self.store.save()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_default_concurrent_launches_are_isolated_and_dirty_source_is_preserved(self):
        (self.repo / "tracked.txt").write_text("uncommitted")
        (self.repo / "secret.env").write_text("private fixture")
        before = git(self.repo, "status", "--porcelain")
        first, second = await asyncio.gather(
            session_directory(self.store, self.project),
            session_directory(self.store, self.project),
        )
        self.assertNotEqual(first, second)
        for cwd in (first, second):
            self.assertEqual((cwd / "tracked.txt").read_text(), "committed")
            self.assertFalse((cwd / "secret.env").exists())
            self.assertEqual(git(cwd, "rev-parse", "HEAD"), git(self.repo, "rev-parse", "HEAD"))
            self.assertTrue(git(cwd, "branch", "--show-current").startswith("chert/"))
        (first / "tracked.txt").write_text("first session")
        self.assertEqual((second / "tracked.txt").read_text(), "committed")
        self.assertEqual((self.repo / "tracked.txt").read_text(), "uncommitted")
        self.assertEqual(git(self.repo, "status", "--porcelain"), before)
        loaded = ProjectStore(self.store.path)
        self.assertTrue(loaded.projects["work"].worktrees)
        self.assertEqual(loaded.for_directory(first / "sub").name, "work")

    async def test_overrides_do_not_change_default_and_non_git_can_opt_out(self):
        self.assertEqual(await session_directory(self.store, self.project, False), self.repo)
        self.assertTrue(self.project.worktrees)
        self.project.worktrees = False
        cwd = await session_directory(self.store, self.project, True)
        self.assertNotEqual(cwd, self.repo)
        self.assertFalse(self.project.worktrees)
        self.project.directory = str(self.root)
        self.assertEqual(await session_directory(self.store, self.project), self.root)
        with self.assertRaisesRegex(ValueError, "worktree"):
            await session_directory(self.store, self.project, True)

    async def test_subproject_and_managed_routing_win_over_containing_project(self):
        self.project.directory = str(self.repo / "sub")
        self.store.projects["host"] = Project("host", str(self.root), 456)
        cwd = await session_directory(self.store, self.project)
        self.assertEqual(cwd.name, "sub")
        self.assertEqual((cwd / "file.txt").read_text(), "subproject")
        self.assertIs(self.store.for_directory(cwd), self.project)
        self.assertIs(self.store.for_directory(cwd.parent), self.project)

    async def test_uncommitted_subproject_fails_without_creating_a_worktree(self):
        sub = self.repo / "untracked"
        sub.mkdir()
        self.project.directory = str(sub)
        with self.assertRaises(ValueError):
            await session_directory(self.store, self.project)
        self.assertFalse(self.store.worktree_root(self.project).exists())

    async def test_legacy_registry_defaults_to_off(self):
        data = json.loads(self.store.path.read_text())
        del data["projects"][0]["worktrees"]
        self.store.path.write_text(json.dumps(data))
        self.assertFalse(ProjectStore(self.store.path).projects["work"].worktrees)


class WorktreeFrontendTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_projects.ProjectFrontendTests.asyncSetUp
    asyncTearDown = test_projects.ProjectFrontendTests.asyncTearDown
    interaction = test_projects.ProjectFrontendTests.interaction
    message = test_projects.ProjectFrontendTests.message

    async def test_settings_schema_persistence_permissions_and_failed_save(self):
        project = self.projects.projects["one"]
        repository(Path(project.directory))
        command = self.bot.tree.get_command("worktrees")
        self.assertFalse(command._params["enabled"].required)
        await command._do_call(self.interaction(), {"enabled": True})
        self.assertIn("New threads: worktree", self.channels[100].edit.call_args.kwargs["topic"])
        self.assertTrue(ProjectStore(self.projects.path).projects["one"].worktrees)
        self.assertFalse(self.projects.projects["two"].worktrees)

        await command._do_call(self.interaction(user=999), {"enabled": False})
        self.assertTrue(project.worktrees)
        with patch.object(self.projects, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.bot.project_commands.set_worktrees(project, False)
        self.assertTrue(project.worktrees)
        await command._do_call(self.interaction(), {"enabled": None})
        self.assertTrue(project.worktrees)
        with self.assertRaises(ValueError):
            await self.bot.project_commands.set_worktrees(self.projects.projects["two"], True)
        self.assertFalse(self.projects.projects["two"].worktrees)

    async def test_channel_topic_shows_default_and_skips_unchanged_edits(self):
        project = self.projects.projects["one"]
        self.assertIn("New threads: project directory", project.topic)
        project.worktrees = True
        await self.bot.project_commands.sync_topic(project)
        self.assertEqual(self.channels[100].edit.call_args.kwargs["topic"], project.topic)
        self.channels[100].topic = project.topic
        self.channels[100].edit.reset_mock()
        await self.bot.project_commands.sync_topic(project)
        self.channels[100].edit.assert_not_called()
        await self.bot.project_commands.set_worktrees(project, False)
        self.assertIn("New threads: project directory", self.channels[100].edit.call_args.kwargs["topic"])

    async def test_overrides_route_once_using_project_harness_and_enforce_permissions(self):
        self.bot.codex.launch = AsyncMock()
        self.bot.claude.launch = AsyncMock()
        for name, value in (("worktree", True), ("no-worktree", False)):
            command = self.bot.tree.get_command(name)
            self.assertTrue(command._params["prompt"].required)
            for channel, backend in ((100, self.bot.codex), (200, self.bot.claude)):
                await command._do_call(self.interaction(channel), {"prompt": "do a thing"})
                self.assertEqual(backend.launch.call_args.kwargs, {"worktree": value})
                self.assertEqual(backend.launch.call_args.args[1], "do a thing")
                backend.launch.reset_mock()
                await command._do_call(self.interaction(channel, user=999), {"prompt": "blocked"})
                backend.launch.assert_not_called()
            self.projects.projects["one"].archived = True
            await command._do_call(self.interaction(), {"prompt": "blocked"})
            self.bot.codex.launch.assert_not_called()
            self.projects.projects["one"].archived = False
        self.assertFalse(self.projects.projects["one"].worktrees)

    async def test_plain_prompt_launch_persists_worktree_cwd_and_correct_discord_parent(self):
        project = self.projects.projects["one"]
        repository(Path(project.directory))
        await self.bot.project_commands.set_worktrees(project, True)
        source = self.message(content="work on this")
        thread = SimpleNamespace(id=300, parent_id=100, mention="<#300>")
        source.create_thread = AsyncMock(return_value=thread)
        source.add_reaction = AsyncMock()

        async def call(method, params):
            if method == "thread/start":
                return {"thread": {"id": "native", "cwd": params["cwd"]}}
            return {}

        self.bot.codex.live = SimpleNamespace(
            connect=AsyncMock(), call=AsyncMock(side_effect=call),
            close=AsyncMock(), subscribed=set(),
        )
        self.bot.codex.say = AsyncMock(return_value=SimpleNamespace(id=900))
        self.bot.codex.send_prompt = AsyncMock()
        self.bot.save_attachments = AsyncMock(return_value="")
        await self.bot.on_message(source)
        session = self.bot.codex.store.sessions[300]
        cwd = Path(session.cwd)
        self.assertNotEqual(cwd, Path(project.directory))
        self.assertTrue((cwd / "tracked.txt").is_file())
        self.assertEqual(self.bot.codex.live.call.call_args_list[0].args[1]["cwd"], str(cwd))
        source.create_thread.assert_awaited_once()
        await self.bot.project_commands.set_worktrees(project, False)
        self.assertEqual(self.bot.codex.store.sessions[300].cwd, str(cwd))
        self.assertIs(await self.bot.codex.discovery_channel({"cwd": str(cwd)}), self.channels[100])
        self.assertIn("<#300>", self.bot.project_sessions(project))

    async def test_disabled_claude_creates_no_worktree_and_enabled_claude_uses_it(self):
        project = self.projects.projects["one"]
        repository(Path(project.directory))
        project.worktrees = True
        self.bot.claude_enabled = False
        await self.bot.claude.launch(project, "hello", SimpleNamespace(id=7), AsyncMock())
        self.assertFalse(self.projects.worktree_root(project).exists())
        self.bot.claude_enabled = True
        self.bot.await_registration = AsyncMock(return_value=({"key": "test"}, ""))
        self.bot.adopt_session = AsyncMock(return_value=SimpleNamespace(mention="<#300>"))
        with patch.object(upstream, "spawn_claude", return_value=("pane", "")) as spawn, \
             patch.object(self.bot, "deliver", return_value=(True, "")):
            await self.bot.claude.launch(project, "hello", SimpleNamespace(id=7), AsyncMock())
        cwd = Path(spawn.call_args.args[1])
        self.assertTrue((cwd / "tracked.txt").is_file())
        self.assertNotEqual(cwd, Path(project.directory))

    async def test_failed_codex_launch_retains_workspace_and_reports_location(self):
        project = self.projects.projects["one"]
        repository(Path(project.directory))
        project.worktrees = True
        self.bot.codex.start_session = AsyncMock(side_effect=RuntimeError("native unavailable"))
        with self.assertRaisesRegex(RuntimeError, "Worktree retained at"):
            await self.bot.codex.launch(project, "hello", SimpleNamespace(id=7), AsyncMock())
        cwd = self.bot.codex.start_session.call_args.kwargs["cwd"]
        self.assertTrue((cwd / "tracked.txt").is_file())

    async def test_worktree_creation_failure_never_falls_back_to_project_directory(self):
        project = self.projects.projects["one"]
        project.worktrees = True
        self.bot.codex.start_session = AsyncMock()
        with self.assertRaises(ValueError):
            await self.bot.codex.launch(project, "hello", SimpleNamespace(id=7), AsyncMock())
        self.bot.codex.start_session.assert_not_called()
