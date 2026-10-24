from __future__ import annotations

from dataclasses import replace
import asyncio
import logging

from aiohttp import web

from tgvio_player.domain.storage_settings import safe_storage_relpath

_LOG = logging.getLogger("tgvio_player.status")


class PlayerStatusHttpMixin:
    async def _status(self, request: web.Request) -> web.Response:
        """One page of facts a non-technical owner can act on."""
        await self._authenticate(request)
        settings = await self._repository.get_storage_settings()
        libraries = []
        for root in await self._repository.get_libraries():
            reachable = None
            if self._archive_check is not None:
                try:
                    await asyncio.wait_for(self._archive_check(replace(settings, archive_root=root)), timeout=15)
                    reachable = True
                except Exception as exc:
                    _LOG.info("player.status.library_unreachable error=%s", type(exc).__name__)
                    reachable = False
            libraries.append({"root": root, "reachable": reachable})
        summary = await self._repository.favorite_sync_summary()
        sync = self._sync_status
        return web.json_response({
            "libraries": libraries,
            "scan": None if sync is None else {
                "last_full_at": sync.last_full_at, "last_error_at": sync.last_error_at,
                "active_videos": sync.active_videos, "rejected": sync.rejected,
            },
            "probe_pending": len(await self._repository.list_unprobed_video_ids(limit=10_000)),
            "backup": {
                "pending": int(summary["pending"] or 0), "failed": int(summary["failed"] or 0),
                "last_success_at": summary["last_success_at"],
            },
        })

    async def _folders(self, request: web.Request) -> web.Response:
        """Subfolders of one WebDAV folder, so the settings page can pick instead of type."""
        await self._authenticate(request)
        if self._list_folders is None:
            raise web.HTTPServiceUnavailable(text="folder browser unavailable")
        raw = request.query.get("path", "").strip().strip("/")
        try:
            path = safe_storage_relpath(raw) if raw else ""
        except ValueError:
            raise web.HTTPBadRequest(text="invalid folder") from None
        try:
            names = await asyncio.wait_for(self._list_folders(path), timeout=20)
        except Exception as exc:
            _LOG.info("player.folders.list_failed error=%s", type(exc).__name__)
            raise web.HTTPBadGateway(text="folder could not be listed") from None
        return web.json_response({"path": path, "folders": names})
