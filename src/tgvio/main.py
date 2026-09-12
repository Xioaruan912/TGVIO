from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from tgvio.adapters.telegram.bot_ui import TelethonBotUI
from tgvio.adapters.telegram.discussion_resolver import BotApiDiscussionResolver
from tgvio.adapters.telegram.intake_runtime import TelethonIntakeRuntime
from tgvio.adapters.telegram.media_downloader import TelethonMediaDownloader
from tgvio.adapters.telegram.publish_transport import TelethonPublishTransport
from tgvio.adapters.telegram.telethon_gateway import TelethonGateway
from tgvio.adapters.url_downloader import UrlMediaDownloader
from tgvio.adapters.webdav_archive import WebDavArchiveTransport
from tgvio.application.archive_planner import ArchivePlanner
from tgvio.application.archive_runtime import ArchiveRuntime, ArchiveService
from tgvio.application.cache_cleanup import CacheCleanupRuntime, CacheCleanupService
from tgvio.application.execution import PublishExecutionEngine
from tgvio.application.intake import IntakeService
from tgvio.application.job_diagnostics import JobDiagnosticService
from tgvio.application.job_control import JobControlService
from tgvio.application.job_runner import JobRunner
from tgvio.application.media_analyzer import MediaAnalyzer
from tgvio.application.media_downloader import JobDownloader
from tgvio.application.media_router import RoutedMediaDownloader
from tgvio.application.orchestrator import JobOrchestrator, PlanningPolicy
from tgvio.application.processor import IngestionProcessor
from tgvio.application.reference_cache import TelegramReferenceEnricher
from tgvio.application.runtime_health import RuntimeHealthHeartbeat
from tgvio.config import Settings, load_dotenv
from tgvio.infrastructure.media_inspector import FFprobeMediaInspector
from tgvio.infrastructure.log_reader import JsonlOperationalLogReader
from tgvio.infrastructure.media_transformer import FFmpegMediaTransformer
from tgvio.infrastructure.sqlite import SQLiteJobRepository
from tgvio.domain.job import JobState
from tgvio.observability import configure_logging, log_event


async def run(*, check_only: bool = False) -> None:
    load_dotenv()
    settings = Settings.from_env()
    configure_logging(
        level=settings.log_level,
        log_dir=settings.log_dir,
        file_enabled=settings.log_file_enabled,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
    )
    logger = logging.getLogger("tgvio")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    repository = SQLiteJobRepository(settings.data_dir / "state.sqlite3")
    await repository.open()
    try:
        schema_status = repository.schema_status()
        await repository.set_runtime_health("schema", "ready", detail=schema_status)
        log_event(
            logger,
            logging.INFO,
            "runtime.bootstrap.ready",
            "TGVIO bootstrap ready",
            environment=settings.environment,
            publish_enabled=settings.publish_enabled,
            archive_enabled=settings.archive_enabled,
            url_enabled=settings.url_enabled,
            worker_concurrency=settings.worker_concurrency,
            app_commit=os.getenv("APP_COMMIT", "unknown"),
            schema_version=schema_status["latest_version"],
            migration_ledger=schema_status["ledger_present"],
        )
        if check_only or not settings.run_bot:
            log_event(
                logger,
                logging.INFO,
                "runtime.check.completed",
                "Telegram runtime disabled; foundation check complete",
            )
            return
        gateway = TelethonGateway(settings, Path("/app/session"))
        await gateway.start()
        runtime_health = RuntimeHealthHeartbeat(
            repository,
            gateway.client.is_connected,
        )
        await runtime_health.start()
        cache_runtime = CacheCleanupRuntime(
            CacheCleanupService(
                repository,
                settings.download_dir,
                retention_hours=settings.cache_retention_hours,
            ),
            interval_minutes=settings.cache_cleanup_interval_minutes,
        )
        await cache_runtime.start()
        archive_runtime = None
        if settings.archive_enabled:
            archive_service = ArchiveService(
                repository,
                ArchivePlanner(remote_root=settings.archive_remote_root),
                WebDavArchiveTransport(
                    settings.archive_url,
                    settings.archive_user,
                    settings.archive_password,
                    capability_root=settings.archive_remote_root,
                    response_timeout=600.0,
                ),
            )
            archive_runtime = ArchiveRuntime(
                archive_service,
                poll_seconds=settings.archive_poll_seconds,
            )
        control = JobControlService(repository)
        intake = IntakeService(repository)
        routed_downloader = RoutedMediaDownloader(
            TelethonMediaDownloader(
                gateway.client,
                download_workers=settings.telegram_download_workers,
                part_size_kb=settings.telegram_part_size_kb,
                shard_retries=settings.telegram_shard_retries,
            ),
            {
                "url": UrlMediaDownloader(
                    private_network_policy=settings.url_private_network_policy,
                )
            },
        )
        downloader = JobDownloader(
            repository,
            routed_downloader,
            settings.download_dir,
            reserve_bytes=settings.disk_reserve_bytes,
            control=control,
        )
        analyzer = MediaAnalyzer(repository, FFprobeMediaInspector(), control)
        orchestrator = JobOrchestrator(
            repository,
            PlanningPolicy(
                cover_mode=settings.cover_mode,
                cover_limit=10,
                album_limit=10,
                forward_caption=settings.forward_caption,
                caption_footer=" ".join(
                    value
                    for value in (settings.channel_at, settings.group_at)
                    if value
                ).strip(),
            ),
        )
        processor = IngestionProcessor(
            downloader,
            analyzer,
            orchestrator,
            TelegramReferenceEnricher(repository),
            control,
        )
        execution = None
        if settings.publish_enabled or settings.live_fixture_enabled:
            execution = PublishExecutionEngine(
                repository,
                TelethonPublishTransport(
                    gateway.client,
                    FFmpegMediaTransformer(),
                    settings.download_dir,
                    cover_width=settings.cover_width,
                    split_part_bytes=settings.upload_part_bytes,
                    discussion_resolver=BotApiDiscussionResolver(settings.bot_token, timeout_seconds=45.0, poll_interval_seconds=1.0),
                ),
                control,
            )
        runner = JobRunner(
            repository,
            processor,
            execution if settings.publish_enabled else None,
            archive_runtime,
        )
        intake_runtime = TelethonIntakeRuntime(
            gateway.client,
            settings,
            intake,
            runner,
        )
        bot_ui = TelethonBotUI(
            gateway.client,
            settings,
            repository,
            fixture_execution=(
                execution if settings.live_fixture_enabled else None
            ),
            control=control,
            schedule_job=intake_runtime.schedule,
            archive_operator=archive_runtime,
            cache_operator=cache_runtime,
            job_diagnostics=JobDiagnosticService(
                repository,
                JsonlOperationalLogReader(settings.log_dir)
                if settings.log_file_enabled
                else None,
            ),
        )
        bot_ui.register()
        await bot_ui.configure_server_menu()
        intake_runtime.register()
        if archive_runtime is not None:
            await archive_runtime.start()
        recoverable = await repository.list_by_states(
            (
                JobState.RECEIVED,
                JobState.DOWNLOADING,
                JobState.DOWNLOADED,
                JobState.ANALYZING,
                JobState.ANALYZED,
                *((JobState.PLANNED, JobState.PUBLISHING) if settings.publish_enabled else ()),
            )
        )
        for job in recoverable:
            intake_runtime.schedule(job)
        try:
            log_event(
                logger,
                logging.INFO,
                "runtime.telegram.ready",
                "Telegram adapter connected; intake runtime active",
                recovery_jobs=len(recoverable),
            )
            await gateway.client.run_until_disconnected()
        finally:
            await intake_runtime.stop()
            await bot_ui.stop()
            if archive_runtime is not None:
                await archive_runtime.stop()
            await cache_runtime.stop()
            await runtime_health.stop()
            await gateway.stop()
    finally:
        await repository.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="validate foundation without starting Telegram")
    args = parser.parse_args()
    asyncio.run(run(check_only=args.check))


if __name__ == "__main__":
    main()

