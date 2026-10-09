from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import ipaddress
import logging
import os
from pathlib import Path
import signal
from urllib.parse import urlsplit

from aiohttp import web

from tgvio_player.adapters.http import PlayerHttpServer
from tgvio_player.application.archive_read import ArchiveReadRouter, ReadModeService
from tgvio_player.application.auth import SessionService
from tgvio_player.application.catalog import CatalogSyncService
from tgvio_player.application.favorite_backup import FavoriteBackupService, SourceMediaError
from tgvio_player.application.faststart import FaststartBackfill, FaststartService
from tgvio_player.application.feed import ShuffleDeckService
from tgvio_player.application.player_recovery import PlayerRecoveryService
from tgvio_player.application.cover_warm import CoverWarm
from tgvio_player.application.range_cache import MediaRangeCache
from tgvio_player.application.warm_backfill import MediaWarmBackfill
from tgvio_player.infrastructure.cover_mirror import CoverMirror, CoverMirrorCounters
from tgvio_player.infrastructure.faststart_store import FaststartStore
from tgvio_player.infrastructure.openlist_direct import DEFAULT_USER_AGENT, OpenListDirectReader, OpenListDirectSettings
from tgvio_player.infrastructure.player_crypto import PlayerStateCipher
from tgvio_player.infrastructure.range_store import RangeStore
from tgvio_player.infrastructure.player_crypto import decode_recovery_key
from tgvio_player.infrastructure.sqlite import PlayerCatalogRepositorySQLite
from tgvio_player.infrastructure.webdav_aiohttp import (
    AioHttpReadOnlyWebDavClient,
    WebDavClientSettings,
)
from tgvio_player.infrastructure.webdav_catalog import WebDavArchiveCatalogSource
from tgvio_player.infrastructure.webdav_read import ReadOnlyWebDavAdapter, WebDavDeleteAdapter
from tgvio_player.infrastructure.player_media_reader import PlayerMediaReader
from tgvio_player.infrastructure.webdav_write import AioHttpWebDavWriteClient


_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PlayerSettings:
    data_dir: Path
    access_secret: str
    recovery_key: str = field(repr=False)
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
    large_video_seconds: int
    cache_bytes: int
    cache_chunk_mb: int
    cache_window_mb: int
    cache_concurrency: int
    warm_all: bool
    warm_head_mb: int
    warm_tail_mb: int
    delete_enabled: bool
    cover_mirror: bool
    cover_mirror_bytes: int
    cover_mirror_batch: int
    cover_mirror_concurrency: int
    cover_mirror_interval_seconds: int
    # Optional fast path: OpenList API for direct links. Empty means WebDAV only.
    openlist_api_url: str = ""
    direct_user_agent: str = DEFAULT_USER_AGENT

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "PlayerSettings":
        values = os.environ if environ is None else environ
        get = lambda name: values.get(f"TGVIO_PLAYER_{name}", "").strip()
        if get("ENABLED").lower() != "true":
            raise ValueError("TGVIO_PLAYER_ENABLED must be true")
        required = ("DATA_DIR", "ACCESS_SECRET", "RECOVERY_KEY", "WEBDAV_URL", "WEBDAV_USER", "WEBDAV_PASSWORD", "REMOTE_ROOT")
        missing = [f"TGVIO_PLAYER_{name}" for name in required if not get(name)]
        if missing:
            raise ValueError("missing required Player settings: " + ", ".join(missing))
        secret = get("ACCESS_SECRET")
        is_numeric_pin = secret.isascii() and secret.isdecimal() and len(secret) == 9
        if len(secret) < 32 and not is_numeric_pin:
            raise ValueError(
                "TGVIO_PLAYER_ACCESS_SECRET must be at least 32 characters or a 9-digit PIN"
            )
        recovery_key = get("RECOVERY_KEY")
        decode_recovery_key(recovery_key)
        host = get("HOST") or "0.0.0.0"
        try:
            ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("TGVIO_PLAYER_HOST must be an IP address") from exc
        return cls(
            Path(get("DATA_DIR")), secret, recovery_key, get("WEBDAV_URL"), get("WEBDAV_USER"),
            get("WEBDAV_PASSWORD"), get("REMOTE_ROOT"), host,
            cls._integer(get("PORT") or "8790", "PORT", 1, 65535),
            cls._integer(get("CATALOG_POLL_SECONDS") or "60", "CATALOG_POLL_SECONDS", 5, 86400),
            cls._integer(get("MAX_STREAMS") or "4", "MAX_STREAMS", 1, 64),
            cls._integer(get("MAX_STREAMS_PER_CLIENT") or "4", "MAX_STREAMS_PER_CLIENT", 1, 16),
            cls._flag(get("FASTSTART_BACKFILL") or "true", "FASTSTART_BACKFILL"),
            cls._integer(get("LARGE_VIDEO_SECONDS") or "300", "LARGE_VIDEO_SECONDS", 30, 86400),
            cls._integer(get("CACHE_BYTES") or str(4 * 1024**3), "CACHE_BYTES", 64 * 1024**2, 512 * 1024**3),
            cls._integer(get("CACHE_CHUNK_MB") or "4", "CACHE_CHUNK_MB", 1, 32),
            cls._integer(get("CACHE_WINDOW_MB") or "32", "CACHE_WINDOW_MB", 1, 256),
            cls._integer(get("CACHE_CONCURRENCY") or "4", "CACHE_CONCURRENCY", 1, 16),
            cls._flag(get("WARM_ALL") or "true", "WARM_ALL"),
            cls._integer(get("WARM_HEAD_MB") or "4", "WARM_HEAD_MB", 1, 512),
            cls._integer(get("WARM_TAIL_MB") or "8", "WARM_TAIL_MB", 0, 512),
            cls._flag(get("DELETE_ENABLED") or "false", "DELETE_ENABLED"),
            cls._flag(get("COVER_MIRROR") or "on", "COVER_MIRROR"),
            cls._integer(get("COVER_MIRROR_BYTES") or str(256 * 1024**2),
                         "COVER_MIRROR_BYTES", 1024**2, 8 * 1024**3),
            cls._integer(get("COVER_MIRROR_BATCH") or "64", "COVER_MIRROR_BATCH", 1, 1024),
            cls._integer(get("COVER_MIRROR_CONCURRENCY") or "2", "COVER_MIRROR_CONCURRENCY", 1, 8),
            cls._integer(get("COVER_MIRROR_INTERVAL_SECONDS") or "30",
                         "COVER_MIRROR_INTERVAL_SECONDS", 5, 3600),
            openlist_api_url=cls._optional_url(get("OPENLIST_API_URL"), "OPENLIST_API_URL"),
            direct_user_agent=get("DIRECT_USER_AGENT") or DEFAULT_USER_AGENT,
        )

    @staticmethod
    def _optional_url(value: str, name: str) -> str:
        if not value:
            return ""
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError(f"TGVIO_PLAYER_{name} must be an http(s) URL without credentials")
        return value.rstrip("/")

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


def build_cover_mirror(
    settings: PlayerSettings,
) -> tuple[CoverMirror | None, CoverMirrorCounters | None]:
    """The mirror is a decision the settings make once, so the switch is testable alone."""
    if not settings.cover_mirror:
        return None, None
    return (
        CoverMirror(settings.data_dir / "covers", budget_bytes=settings.cover_mirror_bytes),
        CoverMirrorCounters(),
    )


async def _catalog_poll(
    sync: CatalogSyncService, source: WebDavArchiveCatalogSource, seconds: int, stop: asyncio.Event
) -> None:
    while not stop.is_set():
        try:
            reads, hits = source.metadata_reads, source.metadata_hits
            result = await sync.sync_once()
            _LOG.info(
                "Player catalog sync completed: active_videos=%s rejected=%s metadata_reads=%s metadata_hits=%s",
                result.active_videos, result.rejected,
                source.metadata_reads - reads, source.metadata_hits - hits,
            )
        except Exception:
            _LOG.exception("Player catalog sync failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


async def _favorite_sync_poll(
    sync: FavoriteBackupService, seconds: int, stop: asyncio.Event
) -> None:
    while not stop.is_set():
        try:
            result = await sync.sync_pending(limit=2)
            if result.processed:
                _LOG.info(
                    "Player favorite backup batch completed: processed=%s synced=%s retried=%s failed=%s",
                    result.processed, result.synced, result.retried, result.failed,
                )
        except Exception:
            _LOG.exception("Player favorite backup worker failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


async def run(settings: PlayerSettings) -> None:
    repository = PlayerCatalogRepositorySQLite(settings.data_dir / "player.sqlite3")
    cipher = PlayerStateCipher(settings.recovery_key)
    write_clients: list[AioHttpWebDavWriteClient] = []

    def storage_client_factory(endpoint: str, username: str, password: str) -> AioHttpWebDavWriteClient:
        client = AioHttpWebDavWriteClient(endpoint, username, password)
        write_clients.append(client)
        return client

    recovery = PlayerRecoveryService(repository, cipher, storage_client_factory)
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
    direct_reader: OpenListDirectReader | None = None
    try:
        await client.open()
        archive_reader = ReadOnlyWebDavAdapter(client)
        await repository.recover_interrupted_favorite_sync()
        storage_settings = await repository.get_storage_settings()
        username, password = recovery.credentials_for(storage_settings)
        favorite_writer = storage_client_factory(
            storage_settings.endpoint_url, username, password,
        )

        async def favorite_source(media_id: str):
            location = await repository.active_media_location(media_id)
            if location is None:
                raise SourceMediaError("source_not_found", 404)
            response = await archive_reader.open_range(location[0], location[1], None)
            if response.status == 404:
                close_body = getattr(response.body, "aclose", None)
                if close_body is not None:
                    await close_body()
                raise SourceMediaError("source_not_found", 404)
            if response.status not in {200, 206}:
                raise SourceMediaError("source_request_failed", response.status)
            if response.content_length is None:
                raise SourceMediaError("source_length_missing")
            return response.content_length, response.content_type, response.body

        async def favorite_source_location(media_id: str):
            location = await repository.active_media_location(media_id)
            return None if location is None else (location[0], location[1])

        favorite_backup = FavoriteBackupService(
            repository, favorite_writer, favorite_source, recovery,
            source_location=favorite_source_location,
        )
        direct_reader = (
            OpenListDirectReader(OpenListDirectSettings(
                settings.openlist_api_url, settings.webdav_user, settings.webdav_password,
                user_agent=settings.direct_user_agent,
            ))
            if settings.openlist_api_url else None
        )
        read_router = ArchiveReadRouter(archive_reader, direct_reader)
        read_mode = ReadModeService(repository, read_router)
        _LOG.info("TGVIO Player read mode: %s (direct available: %s)", await read_mode.load(), direct_reader is not None)
        reader = PlayerMediaReader(
            repository, read_router, repository.get_storage_settings,
            recovery.credentials_for, storage_client_factory,
        )
        deleter = WebDavDeleteAdapter(client) if settings.delete_enabled else None
        catalog_source = WebDavArchiveCatalogSource(client, remote_root=settings.remote_root)
        sync = CatalogSyncService(catalog_source, repository)
        faststart = FaststartService(
            FaststartStore(settings.data_dir / "faststart"), repository, reader
        )
        server_ref: list[object | None] = [None]
        cover_mirror, cover_mirror_counters = build_cover_mirror(settings)
        range_cache = MediaRangeCache(
            RangeStore(
                settings.data_dir / "cache",
                chunk_bytes=settings.cache_chunk_mb * 1024 * 1024,
                max_bytes=settings.cache_bytes,
                # The warmed head of a clip is its own tier with half the budget.
                head_chunks=max(1, -(-settings.warm_head_mb // settings.cache_chunk_mb)) if settings.warm_all else 0,
                head_share=0.5,
            ),
            reader,
            window_bytes=settings.cache_window_mb * 1024 * 1024,
            concurrency=settings.cache_concurrency,
            should_pause=lambda: server_ref[0].active_playback_streams > 0
            if server_ref[0] is not None
            else False,
        )
        range_cache.open()
        server = PlayerHttpServer(
            repository,
            SessionService(repository, access_secret=settings.access_secret),
            ShuffleDeckService(repository, max_duration_seconds=settings.large_video_seconds),
            reader,
            deleter=deleter,
            max_streams=settings.max_streams,
            max_streams_per_client=settings.max_streams_per_client,
            static_dir=Path("/app/player-web"),
            faststart=faststart,
            range_cache=range_cache,
            large_video_seconds=settings.large_video_seconds,
            warm_head_bytes=settings.warm_head_mb * 1024 * 1024,
            warm_tail_bytes=settings.warm_tail_mb * 1024 * 1024,
            favorite_backup=favorite_backup,
            recovery_service=recovery,
            storage_client_factory=storage_client_factory,
            cover_mirror=cover_mirror,
            cover_mirror_counters=cover_mirror_counters,
            read_mode=read_mode,
        )
        server_ref[0] = server
        runner = server.runner()
        await runner.setup()
        site = web.TCPSite(runner, settings.host, settings.port)
        await site.start()
        tasks: list[asyncio.Task[object]] = [
            asyncio.create_task(_catalog_poll(sync, catalog_source, settings.catalog_poll_seconds, stop)),
            asyncio.create_task(_favorite_sync_poll(favorite_backup, 5, stop)),
        ]
        if server.media_deletions is not None:
            # Queued permanent deletes, including any left unfinished by a restart.
            tasks.append(asyncio.create_task(server.media_deletions.run(stop)))
        if settings.faststart_backfill:
            backfill = FaststartBackfill(
                faststart, repository, should_pause=lambda: server.active_playback_streams > 0
            )
            tasks.append(asyncio.create_task(backfill.run(stop)))
            _LOG.info("TGVIO Player faststart backfill enabled")
        if settings.warm_all:
            warm = MediaWarmBackfill(
                range_cache,
                repository,
                head_bytes=settings.warm_head_mb * 1024 * 1024,
                workers=settings.cache_concurrency,
                should_pause=lambda: server.active_playback_streams > 0,
            )
            tasks.append(asyncio.create_task(warm.run(stop)))
            _LOG.info("TGVIO Player media warm backfill enabled")
        if settings.cover_mirror and cover_mirror is not None and cover_mirror_counters is not None:
            # Half the cover lanes at most: warming a cache must never be the reason a
            # viewer's own cover request waits.
            warm_covers = CoverWarm(
                repository,
                reader,
                cover_mirror,
                cover_mirror_counters,
                batch=settings.cover_mirror_batch,
                concurrency=min(
                    settings.cover_mirror_concurrency, max(1, server.cover_lane_limit // 2)
                ),
                catch_up_seconds=settings.cover_mirror_interval_seconds,
            )
            tasks.append(asyncio.create_task(warm_covers.run(stop)))
            _LOG.info(
                "TGVIO Player cover mirror enabled: budget=%s batch=%s",
                settings.cover_mirror_bytes, settings.cover_mirror_batch,
            )
        _LOG.info("TGVIO Player listening on configured Player host and port")
        await stop.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await range_cache.shutdown()
        await runner.cleanup()
    finally:
        for write_client in write_clients:
            await write_client.close()
        await client.close()
        if direct_reader is not None:
            await direct_reader.close()
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
