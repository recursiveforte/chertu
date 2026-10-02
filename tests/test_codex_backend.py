import asyncio
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from codex_backend import CodexRunner, Session, SessionStore, project_path


class StoreTests(unittest.TestCase):
    def test_restart_preserves_identity_but_marks_active_turn_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            store = SessionStore(path)
            store.sessions[12] = Session(12, tmp, 'test', 'codex-id', status='running', turns=3)
            store.save()
            loaded = SessionStore(path)
            self.assertEqual(loaded.sessions[12].codex_thread, 'codex-id')
            self.assertEqual(loaded.sessions[12].status, 'interrupted')
            self.assertEqual(loaded.sessions[12].turns, 3)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_corrupt_state_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            path.write_text('{broken')
            with self.assertRaises((ValueError, OSError)):
                SessionStore(path)
            self.assertEqual(path.read_text(), '{broken')

    def test_recovers_last_good_state_after_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            store = SessionStore(path)
            store.sessions[12] = Session(12, tmp, 'test', 'codex-id')
            store.save()
            store.sessions[12].turns = 1
            store.save()
            path.write_text('{broken')
            recovered = SessionStore(path)
            self.assertEqual(recovered.sessions[12].codex_thread, 'codex-id')
            self.assertEqual(path.with_suffix('.json.bak').stat().st_mode & 0o777, 0o600)

    def test_project_selection_blocks_parent_and_symlink_escapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'projects'
            (root / 'valid').mkdir(parents=True)
            (root / 'escape').symlink_to(Path(tmp), target_is_directory=True)
            self.assertEqual(project_path(root, 'valid'), root.resolve() / 'valid')
            for path in ('..', 'escape', '/etc', 'missing'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    project_path(root, path)


# The fake executable exercises real pipes, stdin, argv, process groups, and logs.
FAKE_CODEX = '''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys, time
args = sys.argv[1:]
prompt = sys.stdin.read()
pathlib.Path('received.json').write_text(json.dumps({'args': args, 'prompt': prompt,
    'cwd': os.getcwd(), 'discord_token_present': 'DISCORD_BOT_TOKEN' in os.environ}))
def event(data):
    print(json.dumps(data), flush=True)
if prompt == 'hang':
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    pathlib.Path('child.pid').write_text(str(child.pid))
    time.sleep(60)
if prompt == 'fail':
    event({'type': 'turn.failed', 'error': {'message': 'Sandbox denied'}})
    sys.exit(1)
if prompt == 'no-completion':
    sys.exit(0)
sys.stderr.write('diagnostic ' * 20000)
print('non-json diagnostic', flush=True)
event({'type': 'thread.started', 'thread_id': '11111111-1111-4111-8111-111111111111'})
event({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'progress commentary'}})
if prompt == 'reconnect':
    event({'type': 'error', 'message': 'Reconnecting'})
event({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Fallback answer'}})
pathlib.Path(args[args.index('--output-last-message') + 1]).write_text('Final answer')
event({'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 2}})
'''


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.executable = self.root / 'codex'
        self.executable.write_text(FAKE_CODEX)
        self.executable.chmod(0o755)
        self.runner = CodexRunner(str(self.executable), log_dir=self.root / 'logs', timeout=10)
        self.session = Session(123, str(self.root), 'test')
        self.events = []

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def on_event(self, event):
        self.events.append(event)

    async def test_first_turn_then_explicit_resume_with_settings_and_private_stdin(self):
        prompt = "Don't execute $(echo shell); @everyone"
        with patch.dict(os.environ, {'DISCORD_BOT_TOKEN': 'test-secret'}):
            result = await self.runner.run(self.session, prompt, self.on_event)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, 'Final answer')
        self.assertEqual(result.usage['output_tokens'], 2)
        received = json.loads((self.root / 'received.json').read_text())
        self.assertEqual(received['prompt'], prompt)
        self.assertNotIn(prompt, received['args'])
        self.assertFalse(received['discord_token_present'])
        self.assertNotIn('resume', received['args'])
        self.assertEqual(self.session.codex_thread, '11111111-1111-4111-8111-111111111111')
        self.session.model, self.session.effort = 'my-model', 'high'
        result = await self.runner.run(self.session, 'follow-up', self.on_event)
        self.assertTrue(result.ok)
        args = json.loads((self.root / 'received.json').read_text())['args']
        self.assertEqual(args[-3:], ['resume', self.session.codex_thread, '-'])
        self.assertLess(args.index('--sandbox'), args.index('resume'))
        self.assertIn('my-model', args)
        self.assertIn('model_reasoning_effort="high"', args)
        for path in (self.root / 'logs').iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    async def test_failure_never_retries_without_sandbox(self):
        result = await self.runner.run(self.session, 'fail', self.on_event)
        self.assertFalse(result.ok)
        self.assertIn('Sandbox denied', result.errors)
        self.assertEqual(len(list((self.root / 'logs').glob('*.events.jsonl'))), 1)

    async def test_exit_zero_without_turn_completed_is_failure(self):
        result = await self.runner.run(self.session, 'no-completion', self.on_event)
        self.assertFalse(result.ok)

    async def test_recoverable_reconnect_is_not_a_failed_turn(self):
        result = await self.runner.run(self.session, 'reconnect', self.on_event)
        self.assertTrue(result.ok)

    async def wait_for_child(self):
        for _ in range(100):
            if (self.root / 'child.pid').exists():
                return int((self.root / 'child.pid').read_text())
            await asyncio.sleep(0.02)
        self.fail('fake Codex never started')

    async def assert_child_stopped(self, pid):
        # On Linux an adopted killed child can briefly be a zombie before init reaps it.
        import subprocess
        for _ in range(100):
            result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True)
            if not result.stdout.strip() or result.stdout.strip().startswith('Z'):
                return
            await asyncio.sleep(0.02)
        self.fail(f'child {pid} still running')

    async def test_cancel_kills_tool_children(self):
        task = asyncio.create_task(self.runner.run(self.session, 'hang', self.on_event))
        pid = await self.wait_for_child()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.assert_child_stopped(pid)

    async def test_timeout_kills_tool_children(self):
        self.runner.timeout = 0.5
        task = asyncio.create_task(self.runner.run(self.session, 'hang', self.on_event))
        pid = await self.wait_for_child()
        result = await task
        self.assertFalse(result.ok)
        self.assertIn('exceeded', result.errors[0])
        await self.assert_child_stopped(pid)
