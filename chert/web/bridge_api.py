"""Authenticated local control API used by hooks and the dashboard."""

import json
from pathlib import Path
from aiohttp import web
from chert.vendor import bridge as upstream


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
        return app

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
                for s in self.frontend.codex.store.sessions.values()
                if s.codex_thread == body.get("sid")
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
        channel = await self.frontend.codex.live_channel(session)
        key = body.get("key")
        if key in {"Escape", "esc"} and not session.terminal_pane:
            await self.frontend.codex.stop(channel)
        elif key:
            await self.frontend.codex.terminal.key(session, key)
            self.frontend.codex.store.save()
        else:
            text = (body.get("text") or "").strip()
            if not text or len(text) > upstream.checkin.MAX_SEND_LEN:
                return web.json_response({"error": "empty or oversized message"}, status=400)
            await self.frontend.codex.send_prompt(channel, text)
        return web.json_response({"ok": True})

    async def codex_screen_http(self, request):
        _, session = await self.session_request(request)
        await self.frontend.codex.ensure_live(session)
        text = await self.frontend.codex.terminal.screen(session, ansi=True)
        self.frontend.codex.store.save()
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
