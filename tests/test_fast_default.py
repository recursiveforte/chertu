import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from chert.backends.codex.state import Session, SessionStore
from chert.projects import ProjectStore
import test_projects


class FastDefaultTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_projects.ProjectFrontendTests.asyncSetUp
    asyncTearDown = test_projects.ProjectFrontendTests.asyncTearDown
    interaction = test_projects.ProjectFrontendTests.interaction

    async def test_schema_persistence_inheritance_and_legacy_registry(self):
        command = self.bot.tree.get_command("fast-default")
        self.assertFalse(command._params["mode"].required)
        self.assertEqual([c.value for c in command._params["mode"].choices], ["on", "off", "inherit"])
        project = self.projects.projects["one"]
        for mode, expected in (("on", True), ("off", False), ("inherit", None)):
            await command._do_call(self.interaction(), {"mode": mode})
            self.assertIs(ProjectStore(self.projects.path).projects["one"].fast_default, expected)
            self.assertIsNone(self.projects.projects["two"].fast_default)
            self.assertIn(f"Codex fast default: {mode}", self.channels[100].edit.call_args.kwargs["topic"])
        data = json.loads(self.projects.path.read_text())
        for row in data["projects"]:
            row.pop("fast_default")
        self.projects.path.write_text(json.dumps(data))
        self.assertIsNone(ProjectStore(self.projects.path).projects["one"].fast_default)
        with patch.object(self.projects, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.bot.project_commands.set_fast_default(project, "on")
        self.assertIsNone(project.fast_default)

    async def test_owner_changes_allowed_user_reads_and_thread_parent_scope(self):
        command = self.bot.tree.get_command("fast-default")
        project = self.projects.projects["one"]
        self.bot.config.allowed_users.add(8)
        for user in (8, 999):
            interaction = self.interaction(user=user)
            await command._do_call(interaction, {"mode": "on"})
            self.assertIsNone(project.fast_default)
            interaction.response.send_message.assert_awaited_once()
        interaction = self.interaction(user=8)
        await command._do_call(interaction, {"mode": ""})
        self.assertIn("inherit", interaction.followup.send.call_args.args[0])
        interaction = self.interaction()
        interaction.channel = SimpleNamespace(id=300, parent_id=100)
        await command._do_call(interaction, {"mode": "on"})
        self.assertTrue(project.fast_default)
        interaction.channel = SimpleNamespace(id=999, parent_id=None)
        await command._do_call(interaction, {"mode": "off"})
        self.assertTrue(project.fast_default)
        interaction.channel = self.channels[100]
        interaction.guild_id = 999
        await command._do_call(interaction, {"mode": "off"})
        self.assertTrue(project.fast_default)

    async def test_new_launches_apply_precedence_to_native_queue_and_preserve_existing(self):
        backend = self.bot.codex
        project = self.projects.projects["one"]
        existing = Session(400, project.directory, "existing", "old", service_tier="default")
        backend.store.sessions[400] = existing
        backend.say = AsyncMock(return_value=SimpleNamespace(id=900))
        backend.events.note_interaction = AsyncMock()
        backend.live.connect = AsyncMock()
        backend.live.attach = AsyncMock()
        backend.live.active_turn = AsyncMock(return_value=None)

        async def call(method, params):
            if method == "thread/start":
                return {"thread": {"id": "native", "cwd": params["cwd"]}}
            if method == "thread/queue/add":
                return {"queuedSubmission": {"id": "queued"}}
            if method == "thread/queue/list":
                return {"data": []}
            return {}

        backend.live.call = AsyncMock(side_effect=call)
        for index, (default, global_tier, expected) in enumerate([
            (True, "default", "fast"), (False, "fast", "default"),
            (None, "fast", "fast"), (None, "default", "default"),
            (None, None, None),
        ]):
            project.fast_default = default
            backend.store.meta["service_tier"] = global_tier
            thread = SimpleNamespace(id=300 + index, parent_id=100, parent=self.channels[100])
            self.channels[100].create_thread.return_value = thread
            # Managed worktrees must inherit the source project's setting.
            cwd = self.projects.worktree_root(project) / str(index)
            backend.live.call.reset_mock()
            await backend.start_session("hello", cwd=cwd)
            updates = [c.args[1] for c in backend.live.call.call_args_list
                       if c.args[0] == "thread/settings/update"]
            self.assertEqual(updates, [{"threadId": "native", "serviceTier": expected}] if expected else [])
            session = backend.store.sessions[thread.id]
            self.assertEqual(session.project_channel, 100)
            self.assertIsNone(session.service_tier)  # Native actor now owns the setting.
            self.assertEqual(existing.service_tier, "default")
            backend.live.call.reset_mock()
            await backend.send_prompt(thread, "follow up")
            self.assertFalse(any(c.args[0] == "thread/settings/update" for c in backend.live.call.call_args_list))
        self.assertEqual(SessionStore(backend.store.path).sessions[400].service_tier, "default")

    async def test_project_default_does_not_override_resumed_native_conversation(self):
        backend = self.bot.codex
        project = self.projects.projects["one"]
        project.fast_default = True
        native_id = "12345678-1234-1234-1234-123456789abc"
        self.channels[100].create_thread.return_value = SimpleNamespace(id=300)
        backend.live.connect = AsyncMock()
        backend.live.call = AsyncMock(return_value={"thread": {"id": native_id, "cwd": project.directory}})
        backend.say = AsyncMock(return_value=SimpleNamespace(id=900))
        await backend.start_session("", codex_id=native_id)
        self.assertIsNone(backend.store.sessions[300].service_tier)


if __name__ == "__main__":
    unittest.main()
