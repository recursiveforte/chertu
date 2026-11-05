"""Authenticated local control API used by hooks and the dashboard."""

import json
from dataclasses import asdict
from pathlib import Path
import discord
from aiohttp import web
from chert.vendor import bridge as upstream
from chert.backends.codex.state import session_key


class BridgeAPI:
    def __init__(self, frontend):
        self.frontend = frontend
        self.runner = None

    async def close(self):
        if self.runner is not None:
            await self.runner.cleanup()

    async def health(self, _request):
        return web.Response(text="ok\n")

    def create_app(self):
        app = web.Application(client_max_size=1_000_000)
        app.router.add_post("/hook", self.frontend.hook_http)
        app.router.add_post("/admin/restart-all", self.frontend.admin_restart_all)
        app.router.add_post("/session-file", self.session_file_http)
        app.router.add_post("/codex/send", self.codex_send_http)
        app.router.add_post("/codex/screen", self.codex_screen_http)
        app.router.add_get("/health", self.health)
        app.router.add_get("/projects", self.projects_http)
        app.router.add_post("/projects", self.create_project_http)
        return app

    async def projects_http(self, request):
        if not self.authorized(request):
            return web.json_response({"error": "bad secret"}, status=403)
        return web.json_response({
            "projects": [self.project_result(p) for p in self.frontend.projects.projects.values()],
            "hosts": [{"name": h.name, "project_root": h.project_root or "~/projects"}
                      for h in self.frontend.hosts.values()],
        })

    def project_result(self, project):
        return {**asdict(project), "url": (
            f"https://discord.com/channels/{self.frontend.projects.guild_id}/{project.channel_id}"
        )}

    async def create_project_http(self, request):
        if not self.authorized(request):
            return web.json_response({"error": "bad secret"}, status=403)
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) - {"name", "directory", "host"}:
                raise ValueError("Expected name, optional directory and optional host.")
            name, directory, host = body.get("name"), body.get("directory"), body.get("host", "local")
            if not isinstance(name, str) or not isinstance(host, str):
                raise ValueError("Name and host must be strings.")
            if directory is not None and (not isinstance(directory, str) or not directory.strip()):
                raise ValueError("Directory must be a nonempty string, or omitted for a new project.")
            project = await self.frontend.project_commands.create_project(name, directory, host)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except (OSError, discord.HTTPException) as exc:
            return web.json_response({"error": str(exc)}, status=503)
        return web.json_response(self.project_result(project), status=201)

    async def start(self):
        self.runner = web.AppRunner(self.create_app(), access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", upstream.HOOK_PORT).start()

    def authorized(self, request):
        secret = self.frontend.hook_secret()
        return bool(secret and request.headers.get("X-Hearth-Secret") == secret)

    async def session_request(self, request):
        if not self.authorized(request):
            raise web.HTTPForbidden(
                text=json.dumps({"error": "bad secret"}), content_type="application/json"
            )
        body = await request.json()
        session = next(
            (
                s
                for s in self.frontend.session_store.sessions.values()
                if session_key(s) == body.get("sid")
            ),
            None,
        )
        if session is None:
            raise web.HTTPNotFound(
                text=json.dumps({"error": "session not found"}), content_type="application/json"
            )
        if session.status == "ended":
            raise web.HTTPConflict(
                text=json.dumps({"error": "session ended; revive it first"}),
                content_type="application/json",
            )
        return body, session

    async def codex_send_http(self, request):
        body, session = await self.session_request(request)
        backend = self.frontend.codex_hosts.get(session.host)
        if backend is None:
            return web.json_response({"error": f"Host {session.host} is not configured"}, status=503)
        channel = await backend.live_channel(session)
        key = body.get("key")
        if key in {"Escape", "esc"} and not session.terminal_pane:
            await backend.stop(channel)
        elif key:
            await backend.terminal.key(session, key)
            backend.store.save()
        else:
            text = (body.get("text") or "").strip()
            if not text or len(text) > upstream.checkin.MAX_SEND_LEN:
                return web.json_response({"error": "empty or oversized message"}, status=400)
            await backend.send_prompt(channel, text)
        return web.json_response({"ok": True})

    async def codex_screen_http(self, request):
        _, session = await self.session_request(request)
        backend = self.frontend.codex_hosts.get(session.host)
        if backend is None:
            return web.json_response({"error": f"Host {session.host} is not configured"}, status=503)
        await backend.ensure_live(session)
        text = await backend.terminal.screen(session, ansi=True)
        backend.store.save()
        return web.json_response(
            {
                "html": upstream.checkin.ansi_to_html(text),
                "status": session.status,
                "name": session.name,
            }
        )

    async def session_file_http(self, request):
        if not self.authorized(request):
            return web.Response(status=403, text="bad secret\n")
        body = await request.json()
        path = body.get("path") or ""
        if not Path(path).is_file():
            return web.Response(status=404, text="file not found\n")
        try:
            await self.frontend.deliver_session_file(
                path,
                body.get("caption") or "",
                body.get("pane"),
                body.get("sid"),
                body.get("cwd"),
                body.get("backend"),
            )
        except ValueError as exc:
            return web.Response(status=409, text=str(exc))
        return web.Response(status=202, text="sent\n")
