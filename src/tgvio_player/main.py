from __future__ import annotations

import asyncio
from dataclasses import dataclass
import ipaddress
import logging
import os
from pathlib import Path
import signal

from aiohttp import web

from tgvio_player.adapters.http import PlayerHttpServer
from tgvio_player.application.auth import SessionService
from tgvio_player.application.catalog import CatalogSyncService
from tgvio_player.application.faststart import FaststartBackfill, FaststartService
from tgvio_player.application.feed import ShuffleDeckService
from tgvio_player.infrastructure.faststart_store import FaststartStore
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite
from tgvio_player.infrastructure.webdav_aiohttp import (
    AioHttpReadOnlyWebDavClient,
    WebDavClientSettings,
)
from tgvio_player.infrastructure.webdav_catalog import WebDavArchiveCatalogSource
from tgvio_player.infrastructure.webdav_read import ReadOnlyWebDavAdapter


_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PlayerSettings:
    data_dir: Path
    access_secret: str
    webdav_url: str
    webdav_user: str
    webdav_password: str
    remote_root: str
    host: str
    port: int
    catalog_poll_seconds: int
    max_streams: int
    max_streams_per_client: int
    faststart_backfill: bool

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "PlayerSettings":
        values = os.environ if environ is None else environ
        get = lambda name: values.get(f"TGVIO_PLAYER_{name}", "").strip()
        if get("ENABLED").lower() != "true":
            raise ValueError("TGVIO_PLAYER_ENABLED must be true")
        required = ("DATA_DIR", "ACCESS_SECRET", "WEBDAV_URL", "WEBDAV_USER", "WEBDAV_PASSWORD", "REMOTE_ROOT")
        missing = [f"TGVIO_PLAYER_{name}" for name in required if not get(name)]
        if missing:
            raise ValueError("missing required Player settings: " + ", ".join(missing))
        secret = get("ACCESS_SECRET")
        is_numeric_pin = secret.isascii() and secret.isdecimal() and len(secret) == 9
        if len(secret) < 32 and not is_numeric_pin:
            raise ValueError(
                "TGVIO_PLAYER_ACCESS_SECRET must be at least 32 characters or a 9-digit PIN"
            )
        host = get("HOST") or "0.0.0.0"
        try:
            ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("TGVIO_PLAYER_HOST must be an IP address") from exc
        return cls(
            Path(get("DATA_DIR")), secret, get("WEBDAV_URL"), get("WEBDAV_USER"),
            get("WEBDAV_PASSWORD"), get("REMOTE_ROOT"), host,
            cls._integer(get("PORT") or "8790", "PORT", 1, 65535),
            cls._integer(get("CATALOG_POLL_SECONDS") or "60", "CATALOG_POLL_SECONDS", 5, 86400),
            cls._integer(get("MAX_STREAMS") or "4", "MAX_STREAMS", 1, 64),
            cls._integer(get("MAX_STREAMS_PER_CLIENT") or "2", "MAX_STREAMS_PER_CLIENT", 1, 16),
            cls._flag(get("FASTSTART_BACKFILL") or "true", "FASTSTART_BACKFILL"),
        )

    @staticmethod
    def _flag(value: str, name: str) -> bool:
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"TGVIO_PLAYER_{name} must be a boolean")

    @staticmethod
    def _integer(value: str, name: str, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(f"TGVIO_PLAYER_{name} must be an integer") from exc
        if not minimum <= parsed <= maximum:
            raise ValueError(f"TGVIO_PLAYER_{name} must be between {minimum} and {maximum}")
        return parsed


async def _catalog_poll(sync: CatalogSyncService, seconds: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            result = await sync.sync_once()
            _LOG.info("Player catalog sync completed: active_videos=%s rejected=%s", result.active_videos, result.rejected)
        except Exception:
            _LOG.exception("Player catalog sync failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


async def run(settings: PlayerSettings) -> None:
    repository = PlayerCatalogRepositorySQLite(settings.data_dir / "player.sqlite3")
    client = AioHttpReadOnlyWebDavClient(WebDavClientSettings(
        settings.webdav_url, settings.webdav_user, settings.webdav_password,
    ))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await repository.open()
    try:
        await client.open()
        reader = ReadOnlyWebDavAdapter(client)
        sync = CatalogSyncService(WebDavArchiveCatalogSource(client, remote_root=settings.remote_root), repository)
        faststart = FaststartService(
            FaststartStore(settings.data_dir / "faststart"), repository, reader
        )
        server = PlayerHttpServer(
            repository, SessionService(repository, access_secret=settings.access_secret),
            ShuffleDeckService(repository), reader,
            max_streams=settings.max_streams, max_streams_per_client=settings.max_streams_per_client,
            static_dir=Path("/app/player-web"), faststart=faststart,
        )
        runner = server.runner()
        await runner.setup()
        site = web.TCPSite(runner, settings.host, settings.port)
        await site.start()
        tasks: list[asyncio.Task[object]] = [
            asyncio.create_task(_catalog_poll(sync, settings.catalog_poll_seconds, stop))
        ]
        if settings.faststart_backfill:
            backfill = FaststartBackfill(
                faststart, repository, should_pause=lambda: server.active_playback_streams > 0
            )
            tasks.append(asyncio.create_task(backfill.run(stop)))
            _LOG.info("TGVIO Player faststart backfill enabled")
        _LOG.info("TGVIO Player listening on configured Player host and port")
        await stop.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()
    finally:
        await client.close()
        await repository.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = PlayerSettings.from_env()
        asyncio.run(run(settings))
    except (ValueError, RuntimeError) as exc:
        _LOG.error("Player startup refused: %s", exc)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
