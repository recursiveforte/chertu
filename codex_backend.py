"""Persistent Codex sessions and a cancellable, streaming `codex exec` transport.

No Discord dependency: process lifecycle and session recovery can be tested offline.
Prompts go through stdin; every follow-up resumes an explicit Codex thread ID.
"""
import asyncio
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import signal
import tempfile
import uuid


@dataclass
class Session:
    discord_thread: int
    cwd: str
    name: str
    codex_thread: str | None = None
    model: str = ''
    effort: str = ''
    status: str = 'idle'
    turns: int = 0
    backend: str = 'exec'
    seen_live_items: list[str] = field(default_factory=list)
    active_turn: str | None = None
    status_message: int | None = None
    status_webhook: bool = False
    source_message: int | None = None
    turn_started: float = 0
    display_model: str = ''


class SessionStore:
    def __init__(self, path: Path):
        self.path = path
        self.sessions: dict[int, Session] = {}
        if path.exists():
            data = json.loads(path.read_text())  # Corrupt state must not be silently replaced.
            if data.get('version') != 1:
                raise ValueError(f'Unsupported session state version in {path}')
            for row in data['sessions']:
                session = Session(**row)
                if session.status == 'running':
                    session.status = 'interrupted'
                self.sessions[session.discord_thread] = session

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=self.path.name + '.', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as file:
                json.dump({'version': 1, 'sessions': [asdict(s) for s in self.sessions.values()]}, file)
                file.write('\n')
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def project_path(root: Path, project: str = '') -> Path:
    root = root.expanduser().resolve()
    path = (root / project).resolve()
    if not path.is_relative_to(root) or not path.is_dir():
        raise ValueError('Project must be an existing directory inside PROJECT_ROOT.')
    return path


@dataclass
class TurnResult:
    text: str
    returncode: int
    errors: list[str]
    usage: dict

    @property
    def ok(self):
        return self.returncode == 0 and not self.errors


class CodexRunner:
    def __init__(self, binary='codex', sandbox='workspace-write', timeout=10800,
                 log_dir=Path('private/codex-logs'), network=False):
        if sandbox not in {'read-only', 'workspace-write', 'danger-full-access'}:
            raise ValueError('Invalid CODEX_SANDBOX')
        if timeout <= 0:
            raise ValueError('CODEX_TURN_TIMEOUT must be positive')
        self.binary, self.sandbox, self.timeout = binary, sandbox, timeout
        self.log_dir, self.network = Path(log_dir), network

    def command(self, session: Session, output: Path):
        # Exec flags must precede the resume subcommand (not all are resume flags).
        args = [self.binary, 'exec', '--json', '--skip-git-repo-check',
                '--sandbox', self.sandbox, '-c', 'approval_policy="never"',
                '-c', f'sandbox_workspace_write.network_access={str(self.network).lower()}',
                '--output-last-message', str(output)]
        if session.model:
            args += ['--model', session.model]
        if session.effort:
            args += ['-c', 'model_reasoning_effort=' + json.dumps(session.effort)]
        if session.codex_thread:
            args += ['resume', session.codex_thread]
        return args + ['-']

    async def run(self, session: Session, prompt: str, on_event):
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        run_id = f'{session.discord_thread}-{uuid.uuid4().hex}'
        base = self.log_dir.resolve() / run_id
        output = base.with_suffix('.answer.txt')
        errors, usage = [], {}
        completed = False
        messages = []
        env = os.environ.copy()
        # The model's command environment must not inherit the Discord bot token.
        for key in ('DISCORD_BOT_TOKEN', 'DASH_PASS', 'HEARTH_HOOK_SECRET'):
            env.pop(key, None)
        proc = None
        with private_file(base.with_suffix('.stderr.log')) as stderr, \
                private_file(base.with_suffix('.events.jsonl')) as events:
            # Precreate the answer file with private permissions; Codex truncates it.
            with private_file(output):
                pass
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self.command(session, output), cwd=session.cwd, env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=stderr, start_new_session=True, limit=8 * 1024 * 1024)

                async def consume():
                    nonlocal usage, completed
                    proc.stdin.write(prompt.encode())
                    try:
                        await proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    finally:
                        proc.stdin.close()
                    async for line in proc.stdout:
                        events.write(line)
                        events.flush()
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeDecodeError):
                            continue
                        if not isinstance(event, dict):
                            continue
                        kind = event.get('type')
                        if kind == 'thread.started' and event.get('thread_id'):
                            session.codex_thread = event['thread_id']
                        elif kind == 'turn.completed':
                            completed, usage = True, event.get('usage') or {}
                        elif kind == 'turn.failed':
                            errors.append(error_text(event.get('error') or event))
                        elif kind == 'item.completed':
                            item = event.get('item') or {}
                            if item.get('type') == 'agent_message' and item.get('text'):
                                messages.append(item['text'])
                        # A top-level error can be a recoverable reconnect notice.
                        # Only turn.failed, process failure, or no completion fail a turn.
                        await on_event(event)
                    return await proc.wait()

                try:
                    rc = await asyncio.wait_for(consume(), timeout=self.timeout)
                except asyncio.TimeoutError:
                    rc = -1
                    errors.append(f'Turn exceeded {self.timeout:g} seconds; stopped.')
                if rc != 0 and not errors:
                    errors.append(f'Codex exited with status {rc}; see {base.name}.stderr.log.')
                if rc == 0 and not completed:
                    errors.append('Codex exited without completing the turn.')
            except OSError as exc:
                rc = -1
                errors.append(f'Could not start Codex: {exc}')
            finally:
                if proc is not None:
                    await stop_process_group(proc)
        text = output.read_text(errors='replace').strip()
        return TurnResult(text or (messages[-1] if messages else ''), rc, errors, usage)


def private_file(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb')


def error_text(value):
    return str(value.get('message', value) if isinstance(value, dict) else value)[:1000]


async def stop_process_group(proc):
    """Stop the whole turn, including tool children holding stdout open."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        await proc.wait()
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except asyncio.TimeoutError:
        pass
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
