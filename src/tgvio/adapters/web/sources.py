"""Source routes: the personal Telegram account, its whitelist, browsing and picking.

Everything goes through the existing SourceCoordinator (personal session) and
the pick-preview thumbnail cache; picked rows enter the normal job pipeline.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from tgvio.adapters.telegram.source_runtime import SourceLoginError
from tgvio.application.console import SPOILER_MODES, ConsoleError
from tgvio.application.intake import IncomingMedia
from tgvio.domain.intake import SpoilerMode
from tgvio.domain.job import MediaKind
from tgvio.infrastructure.url_security import validate_url_syntax
from tgvio.observability import log_event

_MAX_PICK = 100
_PAGE_SIZE = 24
_THUMB_CACHE = "private, max-age=86400"


class ConsoleSourceRoutes:
    _source: Any
    _previews: Any
    _submit_media: Any
    _url_enabled: bool
    _archive_enabled: bool
    _log: logging.Logger

    def _register_source_routes(self, app: web.Application) -> None:
        app.router.add_get("/api/source", self._source_get)
        app.router.add_post("/api/source/login/phone", self._login_phone)
        app.router.add_post("/api/source/login/code", self._login_code)
        app.router.add_post("/api/source/login/password", self._login_password)
        app.router.add_post("/api/source/logout", self._source_logout)
        app.router.add_get("/api/source/dialogs", self._dialogs)
        app.router.add_post("/api/source/chats", self._chat_add)
        app.router.add_delete("/api/source/chats/{index}", self._chat_remove)
        app.router.add_get("/api/source/{index}/media", self._media)
        app.router.add_get("/api/source/{index}/thumb/{message_id}", self._thumb)
        app.router.add_post("/api/source/{index}/ad", self._mark_ad)
        app.router.add_post("/api/submit", self._submit)
        app.router.add_post("/api/submit-url", self._submit_url)

    # ---------------------------------------------------------------- state
    def _source_state(self) -> dict[str, Any]:
        source = self._source
        if source is None:
            return {"available": False}
        return {
            "available": True,
            "active": bool(source.active),
            "awaiting": source.awaiting,
            "status": source.status_line().replace("`", ""),
            "whitelist": source.whitelist(),
            "sources": source.sources() if source.active else [],
        }

    def _require_source(self, *, active: bool = True) -> Any:
        if self._source is None:
            raise ConsoleError("没有配置个人账号来源（TGVIO_SOURCE_SESSION）", status=503)
        if active and not self._source.active:
            raise ConsoleError("请先在「来源」页登录你的 Telegram 账号", status=409)
        return self._source

    async def _source_get(self, request: web.Request) -> web.Response:
        del request
        return web.json_response(self._source_state())

    async def _login_step(self, request: web.Request, step: str) -> web.Response:
        source = self._require_source(active=False)
        payload = await self._json(request)  # type: ignore[attr-defined]
        value = payload.get("value")
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise ConsoleError("请输入内容", status=400)
        try:
            if step == "phone":
                message = await source.request_code(value)
            elif step == "code":
                message = await source.submit_code(value)
            else:
                message = await source.submit_password(value)
        except SourceLoginError as exc:
            raise ConsoleError(str(exc)) from None
        log_event(self._log, logging.INFO, "web.source.login_step", step=step, active=bool(source.active))
        return web.json_response({"message": message.replace("`", ""), **self._source_state()})

    async def _login_phone(self, request: web.Request) -> web.Response:
        return await self._login_step(request, "phone")

    async def _login_code(self, request: web.Request) -> web.Response:
        return await self._login_step(request, "code")

    async def _login_password(self, request: web.Request) -> web.Response:
        return await self._login_step(request, "password")

    async def _source_logout(self, request: web.Request) -> web.Response:
        del request
        source = self._require_source(active=False)
        await source.logout()
        return web.json_response(self._source_state())

    async def _dialogs(self, request: web.Request) -> web.Response:
        del request
        source = self._require_source()
        try:
            dialogs = await source.dialogs()
        except SourceLoginError as exc:
            raise ConsoleError(str(exc)) from None
        listed = set(source.whitelist())
        for dialog in dialogs:
            dialog["listed"] = dialog["id"] in listed or (
                bool(dialog.get("username")) and f"@{dialog['username']}" in listed
            )
        return web.json_response({"dialogs": dialogs})

    async def _chat_add(self, request: web.Request) -> web.Response:
        source = self._require_source()
        payload = await self._json(request)  # type: ignore[attr-defined]
        value = payload.get("value")
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise ConsoleError("请输入 @频道名 或 -100 开头的 ID", status=400)
        try:
            message = await source.add_chat(value.strip())
        except SourceLoginError as exc:
            raise ConsoleError(str(exc)) from None
        return web.json_response({"message": message.replace("`", ""), **self._source_state()})

    async def _chat_remove(self, request: web.Request) -> web.Response:
        source = self._require_source(active=False)
        try:
            await source.remove_chat(int(request.match_info["index"]))
        except (SourceLoginError, ValueError):
            raise ConsoleError("来源不存在", status=404) from None
        return web.json_response(self._source_state())

    # ------------------------------------------------------------- browsing
    def _source_index(self, request: web.Request) -> int:
        try:
            index = int(request.match_info["index"])
        except ValueError:
            raise web.HTTPBadRequest(text="invalid source") from None
        if not 0 <= index < len(self._require_source().sources()):
            raise ConsoleError("来源不存在", status=404)
        return index

    async def _media(self, request: web.Request) -> web.Response:
        source = self._require_source()
        index = self._source_index(request)
        try:
            page = max(0, int(request.query.get("page", "0")))
        except ValueError:
            raise web.HTTPBadRequest(text="invalid page") from None
        summaries, has_more, label = await source.list_media(
            index, page=page, page_size=_PAGE_SIZE, refresh=request.query.get("refresh") == "1"
        )
        return web.json_response({
            "source": index,
            "label": label,
            "page": page,
            "has_more": bool(has_more),
            "items": [
                {
                    "message_id": int(item.message_id),
                    "fingerprint": item.fingerprint,
                    "kind": "video" if item.has_video else "photo" if item.is_photo_only else "other",
                    "item_count": item.item_count,
                    "video_count": item.video_count,
                    "photo_count": item.photo_count,
                    "size_bytes": item.size_bytes,
                    "duration_seconds": item.duration_seconds,
                    "width": item.width,
                    "height": item.height,
                    "date": item.date.isoformat() if item.date is not None else None,
                    "caption": item.caption[:300],
                    "ad": bool(item.is_ad),
                    "done": bool(item.already_submitted),
                }
                for item in summaries
            ],
        })

    async def _thumb(self, request: web.Request) -> web.StreamResponse:
        self._require_source()
        index = self._source_index(request)
        try:
            message_id = int(request.match_info["message_id"])
        except ValueError:
            raise web.HTTPBadRequest(text="invalid message") from None
        if self._previews is None:
            raise web.HTTPNotFound()
        image = await self._previews.thumbnail(index, message_id)
        if image is None:
            raise web.HTTPNotFound()
        request["console_cacheable"] = True
        response = web.FileResponse(image)
        response.headers["Cache-Control"] = _THUMB_CACHE
        return response

    async def _mark_ad(self, request: web.Request) -> web.Response:
        source = self._require_source()
        index = self._source_index(request)
        payload = await self._json(request)  # type: ignore[attr-defined]
        fingerprint, is_ad = payload.get("fingerprint"), payload.get("ad")
        if not isinstance(fingerprint, str) or not fingerprint or not isinstance(is_ad, bool):
            raise ConsoleError("invalid request", status=400)
        if is_ad:
            await source.remember_ad(index, fingerprint)
        else:
            await source.release_fingerprint(index, fingerprint)
        return web.json_response({"ok": True})

    # -------------------------------------------------------------- picking
    def _options(self, payload: dict[str, Any]) -> dict[str, Any]:
        mode = payload.get("mode", "publish")
        if mode not in {"publish", "archive"}:
            raise ConsoleError("unknown mode", status=400)
        if mode == "archive" and not self._archive_enabled:
            raise ConsoleError("没有开启网盘归档，不能只归档", status=409)
        options: dict[str, Any] = {"archive_only": mode == "archive"}
        spoiler = payload.get("spoiler")
        if spoiler is not None:
            try:
                parsed = SpoilerMode(str(spoiler))
            except ValueError:
                raise ConsoleError("unknown spoiler mode", status=400) from None
            if parsed not in SPOILER_MODES:
                raise ConsoleError("unknown spoiler mode", status=400)
            options["spoiler_mode"] = parsed
        return options

    async def _submit(self, request: web.Request) -> web.Response:
        source = self._require_source()
        payload = await self._json(request)  # type: ignore[attr-defined]
        rows = payload.get("items")
        if not isinstance(rows, list) or not rows or len(rows) > _MAX_PICK:
            raise ConsoleError(f"请选择 1 到 {_MAX_PICK} 条", status=400)
        selections: list[tuple[int, int]] = []
        fingerprints: list[str] = []
        count = len(source.sources())
        for row in rows:
            if not isinstance(row, dict):
                raise ConsoleError("invalid item", status=400)
            index, message_id, fingerprint = row.get("source"), row.get("message_id"), row.get("fingerprint", "")
            if (not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < count
                    or not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0
                    or not isinstance(fingerprint, str)):
                raise ConsoleError("invalid item", status=400)
            selections.append((index, message_id))
            fingerprints.append(fingerprint)
        options = self._options(payload)
        merge = bool(payload.get("merge"))
        jobs = items = accepted = skipped = failed = 0
        if merge:
            items, _label, failed, accepted, skipped = await source.grab_selection(
                selections, fingerprints=fingerprints, options=options
            )
            jobs = 1 if accepted else 0
        else:
            for (index, message_id), fingerprint in zip(selections, fingerprints):
                found, _label, ok, skip = await source.grab_message(
                    index, message_id, fingerprint=fingerprint, options=options
                )
                items += found
                accepted += ok
                skipped += skip
                jobs += 1 if ok else 0
                failed += 0 if found else 1
        log_event(self._log, logging.INFO, "web.submit", rows=len(selections), jobs=jobs,
                  accepted=accepted, skipped=skipped, failed=failed, merge=merge,
                  archive_only=options["archive_only"])
        return web.json_response({"jobs": jobs, "items": items, "accepted": accepted,
                                  "skipped": skipped, "failed": failed})

    async def _submit_url(self, request: web.Request) -> web.Response:
        if not self._url_enabled or self._submit_media is None:
            raise ConsoleError("链接下载没有开启", status=409)
        payload = await self._json(request)  # type: ignore[attr-defined]
        url = payload.get("url")
        if not isinstance(url, str) or len(url) > 2048 or not url.strip().lower().startswith(("http://", "https://")):
            raise ConsoleError("请输入 http(s) 链接", status=400)
        url = url.strip()
        try:
            hostname, _port = validate_url_syntax(url)
        except ValueError:
            raise ConsoleError("链接不符合安全策略", status=400) from None
        media = IncomingMedia(
            kind=MediaKind.DOCUMENT,
            source=f"url:{url}",
            caption="",
            metadata={"source_type": "url", "url_hostname": hostname},
        )
        accepted, skipped = await self._submit_media([media], self._options(payload))
        return web.json_response({"accepted": accepted, "skipped": skipped})
