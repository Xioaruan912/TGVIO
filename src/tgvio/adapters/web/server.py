"""The TGVIO web console: authenticated JSON API plus the built front end.

Loopback/Docker-bridge only; nginx terminates TLS for ds.722225.xyz. Every
mutating request needs a session cookie and a same-origin Origin header.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from aiohttp import web

from tgvio.adapters.web.auth import (
    COOKIE,
    ConsoleSessions,
    clear_cookie_value,
    resolve_client,
    set_cookie_value,
)
from tgvio.adapters.web.sources import ConsoleSourceRoutes
from tgvio.application.console import ConsoleError, ConsoleService
from tgvio.observability import log_event

_MAX_JSON_BYTES = 64 * 1024
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data: blob:; connect-src 'self'; style-src 'self'; "
        "script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
_PUBLIC_ASSETS = ("site.webmanifest", "console-icon.svg", "console-icon-192.png", "apple-touch-icon.png")
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class ConsoleServer(ConsoleSourceRoutes):
    def __init__(
        self,
        console: ConsoleService,
        sessions: ConsoleSessions,
        *,
        host: str,
        port: int,
        static_dir: Path | None = None,
        source: Any | None = None,
        previews: Any | None = None,
        submit_media: Callable[..., Awaitable[tuple[int, int]]] | None = None,
        url_enabled: bool = False,
        archive_enabled: bool = False,
    ) -> None:
        self._console = console
        self._sessions = sessions
        self._host = host
        self._port = int(port)
        self._static_dir = static_dir
        self._source = source
        self._previews = previews
        self._submit_media = submit_media
        self._url_enabled = bool(url_enabled)
        self._archive_enabled = bool(archive_enabled)
        self._runner: web.AppRunner | None = None
        self._log = logging.getLogger("tgvio.web.console")

    # ------------------------------------------------------------- lifecycle
    def application(self) -> web.Application:
        app = web.Application(client_max_size=_MAX_JSON_BYTES, middlewares=[self._guard])
        app.on_response_prepare.append(self._headers)
        app.router.add_get("/healthz", self._healthz)
        app.router.add_post("/api/login", self._login)
        app.router.add_post("/api/logout", self._logout)
        app.router.add_get("/api/session", self._session)
        app.router.add_get("/api/status", self._status)
        app.router.add_post("/api/queue/pause", self._queue_pause)
        app.router.add_post("/api/queue/resume", self._queue_resume)
        app.router.add_post("/api/cache/clean", self._cache_clean)
        app.router.add_get("/api/jobs", self._jobs)
        app.router.add_post("/api/jobs/retry-all", self._retry_all)
        app.router.add_get("/api/jobs/{job_id}", self._job)
        app.router.add_post("/api/jobs/{job_id}/{action}", self._job_action)
        app.router.add_get("/api/settings", self._settings_get)
        app.router.add_put("/api/settings", self._settings_put)
        self._register_source_routes(app)
        if self._static_dir is not None:
            app.router.add_get("/", self._index)
            assets = self._static_dir / "assets"
            if assets.is_dir():
                app.router.add_static("/assets", assets, show_index=False, follow_symlinks=False)
            for name in _PUBLIC_ASSETS:
                app.router.add_get(f"/{name}", self._public_asset(name))
        return app

    async def start(self) -> None:
        self._runner = web.AppRunner(self.application(), access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self._host, self._port).start()
        log_event(self._log, logging.INFO, "web.console.listening", "Web console listening", port=self._port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ------------------------------------------------------------ middleware
    @web.middleware
    async def _guard(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        if "token" in request.query or "access_token" in request.query:
            raise web.HTTPBadRequest(text="query tokens are not accepted")
        path = request.path
        if request.method not in {"GET", "HEAD"}:
            self._same_origin(request)
        if (path.startswith("/api/") and path not in {"/api/login", "/api/session"}
                and not self._sessions.valid(request.cookies.get(COOKIE))):
            raise web.HTTPUnauthorized(text="login required")
        try:
            return await handler(request)
        except ConsoleError as exc:
            return web.json_response({"error": str(exc)}, status=exc.status)

    async def _headers(self, request: web.Request, response: web.StreamResponse) -> None:
        response.headers.update(_SECURITY_HEADERS)
        if request.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif request.path.startswith("/api/") and not request.get("console_cacheable"):
            response.headers["Cache-Control"] = "no-store"
        elif not request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache"

    @staticmethod
    def _same_origin(request: web.Request) -> None:
        origin = request.headers.get("Origin")
        if not origin or origin == "null":
            raise web.HTTPForbidden(text="same-origin request required")
        parsed = urlsplit(origin)
        allowed = {request.host.lower()}
        forwarded = request.headers.get("X-Forwarded-Host")
        if forwarded:
            allowed.add(forwarded.split(",", 1)[0].strip().lower())
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in allowed:
            raise web.HTTPForbidden(text="cross-origin request rejected")

    @staticmethod
    async def _json(request: web.Request) -> dict[str, Any]:
        try:
            value = await request.json(loads=json.loads)
        except Exception:
            raise web.HTTPBadRequest(text="invalid JSON") from None
        if not isinstance(value, dict):
            raise web.HTTPBadRequest(text="JSON object required")
        return value

    # ------------------------------------------------------------------ auth
    async def _healthz(self, request: web.Request) -> web.Response:
        del request
        return web.json_response({"ok": True})

    async def _login(self, request: web.Request) -> web.Response:
        payload = await self._json(request)
        token = self._sessions.login(resolve_client(request), payload.get("secret"))
        if token is None:
            raise web.HTTPUnauthorized(text="invalid secret")
        response = web.json_response({"authenticated": True})
        response.headers.add("Set-Cookie", set_cookie_value(token))
        return response

    async def _logout(self, request: web.Request) -> web.Response:
        self._sessions.logout(request.cookies.get(COOKIE))
        response = web.json_response({"authenticated": False})
        response.headers.add("Set-Cookie", clear_cookie_value())
        return response

    async def _session(self, request: web.Request) -> web.Response:
        return web.json_response({"authenticated": self._sessions.valid(request.cookies.get(COOKIE))})

    # ------------------------------------------------------- status and jobs
    async def _status(self, request: web.Request) -> web.Response:
        del request
        status = await self._console.status()
        status["source"] = self._source_state()
        status["url_enabled"] = self._url_enabled
        return web.json_response(status)

    async def _queue_pause(self, request: web.Request) -> web.Response:
        del request
        return web.json_response(await self._console.pause_queue())

    async def _queue_resume(self, request: web.Request) -> web.Response:
        del request
        return web.json_response(await self._console.resume_queue())

    async def _cache_clean(self, request: web.Request) -> web.Response:
        del request
        return web.json_response(await self._console.clean_cache())

    async def _jobs(self, request: web.Request) -> web.Response:
        try:
            page = max(0, int(request.query.get("page", "0")))
        except ValueError:
            raise web.HTTPBadRequest(text="invalid page") from None
        return web.json_response(await self._console.jobs(request.query.get("filter", "running"), page))

    async def _job(self, request: web.Request) -> web.Response:
        return web.json_response(await self._console.job(request.match_info["job_id"]))

    async def _job_action(self, request: web.Request) -> web.Response:
        job_id, action = request.match_info["job_id"], request.match_info["action"]
        actions = {
            "retry": self._console.retry,
            "cancel": self._console.cancel,
            "hold": self._console.hold,
            "resume": self._console.resume,
            "retry-archive": self._console.retry_archive,
        }
        if action not in actions:
            raise web.HTTPNotFound(text="unknown action")
        return web.json_response(await actions[action](job_id))

    async def _retry_all(self, request: web.Request) -> web.Response:
        del request
        return web.json_response(await self._console.retry_all())

    async def _settings_get(self, request: web.Request) -> web.Response:
        del request
        return web.json_response(await self._console.settings())

    async def _settings_put(self, request: web.Request) -> web.Response:
        return web.json_response(await self._console.update_settings(await self._json(request)))

    # -------------------------------------------------------------- frontend
    async def _index(self, request: web.Request) -> web.FileResponse:
        del request
        assert self._static_dir is not None
        index = self._static_dir / "index.html"
        if not index.is_file():
            raise web.HTTPNotFound(text="frontend unavailable")
        return web.FileResponse(index)

    def _public_asset(self, name: str) -> Handler:
        async def serve(request: web.Request) -> web.FileResponse:
            del request
            assert self._static_dir is not None
            asset = self._static_dir / name
            if not asset.is_file():
                raise web.HTTPNotFound()
            return web.FileResponse(asset)

        return serve
