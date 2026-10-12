"""Named execution hosts; SSH keeps remote Codex sockets off the network."""

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
import tempfile


@dataclass(frozen=True)
class Host:
    name: str
    ssh: str = ""
    project_root: str = ""
    socket: str = ""
    python: str = "python3"
    codex: str = "codex"
    runtime: str = "~/.local/share/chert"
    path: str = ""

    def __post_init__(self):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", self.name):
            raise ValueError("Host names must use 1–32 lowercase letters, numbers, - or _.")
        if self.name != "local" and not self.ssh:
            raise ValueError(f"Host {self.name} needs an SSH destination.")
        if self.ssh and (self.ssh.startswith("-") or any(c.isspace() for c in self.ssh)):
            raise ValueError("Use an SSH config alias or user@hostname, without options.")
        if self.ssh and (not self.socket.startswith("/") or ":" in self.socket):
            raise ValueError("Remote socket must be an absolute Unix socket path without colons.")

    def ssh_command(self, *args):
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
                *args, self.ssh]


def load_hosts(path, config):
    hosts = {"local": Host("local", project_root=str(config.project_root),
                           socket=str(config.live_socket or ""))}
    if path is None:
        return hosts
    data = json.loads(Path(path).read_text())
    for row in data["hosts"]:
        host = Host(**row)
        if host.name in hosts:
            raise ValueError(f"Duplicate/reserved host name: {host.name}")
        hosts[host.name] = host
    return hosts


class RemoteHost:
    """Short-lived filesystem helpers plus a supervised SSH socket forward."""

    def __init__(self, host):
        self.host = host
        self.process = None
        self.folder = None
        self.lock = asyncio.Lock()

    async def run(self, operation, **params):
        source = Path(__file__).with_name("host_worker.py").read_text()
        command = shlex.join([self.host.python, "-c", source])
        process = await asyncio.create_subprocess_exec(
            *self.host.ssh_command(), command,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(json.dumps({
                "operation": operation, "params": params,
                "runtime": self.host.runtime, "root": self.host.project_root,
                "path": self.host.path,
            }).encode()), 90)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise ConnectionError(f"Host {self.host.name}: {err.decode(errors='replace')[-1200:]}")
        result = json.loads(out)
        if "error" in result:
            raise ValueError(f"Host {self.host.name}: {result['error']}")
        return result["result"]

    async def connect(self):
        async with self.lock:
            if self.process and self.process.returncode is None:
                return Path(self.folder.name) / "codex.sock"
            await self.close()
            self.folder = tempfile.TemporaryDirectory(prefix="chert-ssh-")
            socket = Path(self.folder.name) / "codex.sock"
            self.process = await asyncio.create_subprocess_exec(
                *self.host.ssh_command("-N", "-o", "ExitOnForwardFailure=yes",
                                       "-L", f"{socket}:{self.host.socket}"),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                for _ in range(120):
                    if self.process.returncode is not None:
                        raise ConnectionError(f"SSH connection to {self.host.name} failed.")
                    if socket.exists():
                        return socket
                    await asyncio.sleep(0.1)
                raise TimeoutError(f"SSH connection to {self.host.name} timed out.")
            except BaseException:
                await self.close()
                raise

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None
        if self.folder:
            self.folder.cleanup()
            self.folder = None
