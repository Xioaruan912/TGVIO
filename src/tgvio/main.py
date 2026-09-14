from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
import shutil
import signal

from tgvio.adapters.telegram.bot_ui import TelethonBotUI
from tgvio.adapters.telegram.discussion_resolver import BotApiDiscussionResolver
from tgvio.adapters.telegram.intake_runtime import TelethonIntakeRuntime
from tgvio.adapters.telegram.media_downloader import TelethonMediaDownloader
from tgvio.adapters.telegram.message_remover import TelethonPublishedMessageRemover
from tgvio.adapters.telegram.publish_transport import TelethonPublishTransport
from tgvio.adapters.telegram.telethon_gateway import TelethonGateway
from tgvio.adapters.url_downloader import UrlMediaDownloader
from tgvio.adapters.webdav_archive import WebDavArchiveTransport
from tgvio.application.archive_deletion import ArchiveDeletionService
from tgvio.application.archive_planner import ArchivePlanner
from tgvio.application.archive_runtime import ArchiveRuntime, ArchiveService
from tgvio.application.auto_recovery import (
    AUTO_RECOVERY_POLICY_KEY,
    AutoRecoveryPolicy,
    AutoRecoveryRuntime,
    AutoRecoveryService,
)
from tgvio.application.cache_cleanup import CacheCleanupRuntime, CacheCleanupService
from tgvio.adapters.web.dashboard import DashboardServer
from tgvio.application.dashboard import DashboardService
from tgvio.application.diagnostics import (
    DiagnosticFeatureConfig,
    DiagnosticSnapshotService,
    static_proxy_health_detail,
)
from tgvio.application.metrics import MetricsService
from tgvio.application.alerts import AlertRuntime
from tgvio.application.maintenance import HistoryMaintenanceRuntime, HistoryMaintenanceService
from tgvio.application.runtime_flags import RuntimeFlags
from tgvio.application.notifications import NotificationRuntime, Notifier, WebhookNotifier
from tgvio.adapters.telegram.alerts import TelegramOwnerNotifier
from tgvio.domain.notifications import ALERT_EVENT_TYPES
from tgvio.application.execution import PublishExecutionEngine
from tgvio.application.intake import IntakeService
from tgvio.application.collection_editing import CollectionEditingService
from tgvio.application.job_diagnostics import JobDiagnosticService
from tgvio.application.job_control import JobControlService
from tgvio.application.job_runner import JobRunner
from tgvio.application.media_analyzer import MediaAnalyzer
from tgvio.application.media_downloader import JobDownloader
from tgvio.application.media_router import RoutedMediaDownloader
from tgvio.application.operation_tokens import OperationTokenService
from tgvio.application.orchestrator import JobOrchestrator, PlanningPolicy
from tgvio.application.processor import IngestionProcessor
from tgvio.application.reference_cache import TelegramReferenceEnricher
from tgvio.application.runtime_health import RuntimeHealthHeartbeat
from tgvio.application.scheduler import RuntimeLeaseGuard
from tgvio.application.undo import UndoService
from tgvio.config import Settings, load_dotenv
from tgvio.infrastructure.media_inspector import FFprobeMediaInspector
from tgvio.infrastructure.log_reader import JsonlOperationalLogReader
from tgvio.infrastructure.media_transformer import FFmpegMediaTransformer
from tgvio.infrastructure.capabilities import probe_environment_capabilities
from tgvio.infrastructure.proxy_probe import (
    probe_static_proxy_endpoint,
    static_proxy_unchecked_status,
)
from tgvio.infrastructure.sqlite import SQLiteJobRepository
from tgvio.domain.archive import ArchivePolicy, ArchiveProfileSnapshot
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
    runtime_lease: RuntimeLeaseGuard | None = None
    await repository.open()
    try:
        schema_status = repository.schema_status()
        await repository.set_runtime_health("schema", "ready", detail=schema_status)
        runtime_flags = RuntimeFlags()
        await runtime_flags.load(repository)
        capabilities = probe_environment_capabilities()
        await repository.set_runtime_health(
            "capabilities",
            "ready" if (capabilities.ffmpeg and capabilities.ffprobe) else "degraded",
            detail={
                "ffmpeg": capabilities.ffmpeg,
                "ffprobe": capabilities.ffprobe,
                "yt_dlp": capabilities.yt_dlp,
                "cryptg": capabilities.cryptg,
                "hachoir": capabilities.hachoir,
            },
        )
        log_event(
            logger,
            logging.INFO
            if (capabilities.ffmpeg and capabilities.ffprobe)
            else logging.WARNING,
            "runtime.selfcheck",
            "Environment capability self-check",
            ffmpeg=capabilities.ffmpeg,
            ffprobe=capabilities.ffprobe,
            yt_dlp=capabilities.yt_dlp,
            cryptg=capabilities.cryptg,
            hachoir=capabilities.hachoir,
        )
        proxy_probe = (
            static_proxy_unchecked_status(settings.static_proxy_url)
            if check_only
            else await probe_static_proxy_endpoint(
                settings.static_proxy_url,
                timeout_seconds=settings.static_proxy_probe_timeout_seconds,
            )
        )
        await repository.set_runtime_health(
            "static_proxy",
            proxy_probe.state.value,
            detail=static_proxy_health_detail(
                checked_at_epoch=proxy_probe.checked_at_epoch,
            ),
        )
        log_event(
            logger,
            logging.INFO,
            "runtime.bootstrap.ready",
            "TGVIO bootstrap ready",
            environment=settings.environment,
            publish_enabled=settings.publish_enabled,
            archive_enabled=settings.archive_enabled,
            auto_retry_enabled=settings.auto_retry_enabled,
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
        runtime_lease = RuntimeLeaseGuard(repository)
        await runtime_lease.start()
        gateway = TelethonGateway(settings, Path("/app/session"))
        await gateway.start()
        runtime_health = RuntimeHealthHeartbeat(
            repository,
            gateway.client.is_connected,
        )
        await runtime_health.start()
        cache_service = CacheCleanupService(
            repository,
            settings.download_dir,
            retention_hours=settings.cache_retention_hours,
        )
        cache_runtime = CacheCleanupRuntime(
            cache_service,
            interval_minutes=settings.cache_cleanup_interval_minutes,
        )
        await cache_runtime.start()
        archive_runtime = None
        archive_transport = None
        if settings.archive_enabled:
            archive_transport = WebDavArchiveTransport(
                settings.archive_url,
                settings.archive_user,
                settings.archive_password,
                capability_root=settings.archive_remote_root,
                response_timeout=float(settings.archive_response_timeout_seconds),
                verify_attempts=settings.archive_verify_attempts,
                verify_interval_seconds=float(settings.archive_verify_interval_seconds),
            )
            archive_service = ArchiveService(
                repository,
                ArchivePlanner(
                    remote_root=settings.archive_remote_root,
                    layout=(runtime_flags.get("archive_layout") or settings.archive_layout),
                    profile=ArchiveProfileSnapshot(
                        profile_id=settings.archive_profile_id,
                        policy=ArchivePolicy(settings.archive_policy),
                    ),
                ),
                archive_transport,
            )
            archive_runtime = ArchiveRuntime(
                archive_service,
                poll_seconds=settings.archive_poll_seconds,
            )
        control = JobControlService(repository)
        recovery_policy = AutoRecoveryPolicy(
            enabled=settings.auto_retry_enabled,
            max_attempts=settings.auto_retry_max_attempts,
            base_delay_seconds=settings.auto_retry_base_seconds,
            max_delay_seconds=settings.auto_retry_max_seconds,
        )
        intake = IntakeService(
            repository,
            default_policy={AUTO_RECOVERY_POLICY_KEY: recovery_policy.frozen()},
        )
        operation_tokens = OperationTokenService(repository)
        collection_editing = CollectionEditingService(repository, intake, operation_tokens)
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
                    upload_workers=settings.telegram_upload_workers,
                    upload_global_workers=settings.telegram_upload_global_workers,
                    upload_part_size_kb=settings.telegram_part_size_kb,
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
            flags=runtime_flags,
            editing=collection_editing,
        )
        auto_recovery_runtime = AutoRecoveryRuntime(
            AutoRecoveryService(
                repository,
                control,
                cache_operator=cache_runtime,
                archive_operator=archive_runtime,
            ),
            intake_runtime.recover,
            poll_seconds=settings.auto_retry_poll_seconds,
        )
        archive_deletion_service = (
            ArchiveDeletionService(
                repository,
                archive_transport,
                operation_tokens=operation_tokens,
            )
            if archive_transport is not None
            else None
        )
        undo_service = UndoService(
            repository,
            TelethonPublishedMessageRemover(gateway.client),
            operation_tokens=operation_tokens,
        )
        diagnostic_service = DiagnosticSnapshotService(
            repository,
            features=DiagnosticFeatureConfig(
                run_bot=settings.run_bot,
                publish_enabled=settings.publish_enabled,
                url_enabled=settings.url_enabled,
                url_private_network_policy=settings.url_private_network_policy,
                archive_enabled=settings.archive_enabled,
                archive_profile_id=settings.archive_profile_id,
                archive_policy=settings.archive_policy,
                collections_enabled=settings.collections_enabled,
                auto_retry_enabled=settings.auto_retry_enabled,
                live_fixture_enabled=settings.live_fixture_enabled,
                static_proxy_configured=bool(settings.static_proxy_url),
            ),
            schema_status=repository.schema_status,
            capabilities=lambda: capabilities,
        )
        dashboard_server: DashboardServer | None = None
        notification_runtime: NotificationRuntime | None = None
        alert_runtime: AlertRuntime | None = None
        maintenance_runtime: HistoryMaintenanceRuntime | None = None
        dashboard_service: DashboardService | None = None
        if settings.dashboard_enabled:
            dashboard_service = DashboardService(
                repository,
                diagnostic_service,
                disk_usage=lambda: _disk_snapshot(settings.download_dir),
            )
            dashboard_server = DashboardServer(
                dashboard_service,
                MetricsService(repository, diagnostic_service),
                host=settings.dashboard_host,
                port=settings.dashboard_port,
                token=settings.dashboard_token,
            )
        primary_notifier: Notifier | None = None
        secondary_notifier: Notifier | None = None
        primary_accepts: frozenset[str] | None = None
        alert_user_id = settings.alert_user_id or settings.allowed_users[0]
        primary_notifier = TelegramOwnerNotifier(
            lambda text: gateway.client.send_message(
                alert_user_id,
                text,
                parse_mode="md",
            ),
            enabled=lambda: runtime_flags.bool("alerts_enabled", True),
        )
        primary_accepts = ALERT_EVENT_TYPES
        if settings.webhook_enabled:
            webhook_notifier = WebhookNotifier(
                settings.webhook_url,
                settings.webhook_token,
                timeout_seconds=settings.webhook_timeout_seconds,
            )
            secondary_notifier = webhook_notifier
        notification_runtime = NotificationRuntime(
            repository,
            primary_notifier,
            secondary_notifier=secondary_notifier,
            primary_accepts=primary_accepts,
            poll_seconds=settings.notification_poll_seconds,
            max_attempts=settings.webhook_max_attempts,
        )
        alert_runtime = AlertRuntime(
            repository,
            telegram_connected=gateway.client.is_connected,
            disk_free_bytes=lambda: _disk_free_bytes(settings.download_dir),
            reserve_bytes=settings.disk_reserve_bytes,
            cooldown_seconds=settings.alert_cooldown_seconds,
            poll_seconds=settings.alert_poll_seconds,
            enabled=lambda: runtime_flags.bool("alerts_enabled", True),
        )
        maintenance_runtime = HistoryMaintenanceRuntime(
            HistoryMaintenanceService(
                repository,
                cache_operator=cache_runtime,
                status_cleaner=TelethonPublishedMessageRemover(gateway.client),
                log_dir=settings.log_dir,
            ),
            runtime_flags,
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
            archive_deletion_service=archive_deletion_service,
            cache_operator=cache_runtime,
            job_diagnostics=JobDiagnosticService(
                repository,
                JsonlOperationalLogReader(settings.log_dir)
                if settings.log_file_enabled
                else None,
            ),
            undo_service=undo_service,
            operation_tokens=operation_tokens,
            diagnostic_service=diagnostic_service,
            runtime_flags=runtime_flags,
            intake=intake,
        )
        bot_ui.register()
        await bot_ui.configure_server_menu()
        await intake_runtime.start()
        intake_runtime.register()
        if archive_runtime is not None:
            await archive_runtime.start()
        if dashboard_server is not None:
            await dashboard_server.start()
        if notification_runtime is not None:
            await notification_runtime.start()
        if alert_runtime is not None:
            await alert_runtime.start()
        if maintenance_runtime is not None:
            await maintenance_runtime.start()
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
            await intake_runtime.recover(job)
        await auto_recovery_runtime.start()
        telegram_task: asyncio.Task | None = None
        try:
            log_event(
                logger,
                logging.INFO,
                "runtime.telegram.ready",
                "Telegram adapter connected; intake runtime active",
                recovery_jobs=len(recoverable),
            )
            shutdown_event = asyncio.Event()
            loop = asyncio.get_running_loop()
            registered_signals: list[signal.Signals] = []
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, shutdown_event.set)
                except (NotImplementedError, RuntimeError):
                    continue
                registered_signals.append(sig)
            telegram_task = asyncio.create_task(
                gateway.client.run_until_disconnected(),
                name="tgvio-telegram-disconnect-wait",
            )
            lease_lost_task = asyncio.create_task(
                runtime_lease.wait_lost(),
                name="tgvio-runtime-lease-loss-wait",
            )
            shutdown_task = asyncio.create_task(
                shutdown_event.wait(),
                name="tgvio-runtime-shutdown-wait",
            )
            try:
                done, _pending = await asyncio.wait(
                    {telegram_task, lease_lost_task, shutdown_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if lease_lost_task in done:
                    await gateway.stop()
                    await asyncio.gather(telegram_task, return_exceptions=True)
                    raise RuntimeError("singleton runtime lease lost")
                if shutdown_task in done:
                    log_event(
                        logger,
                        logging.INFO,
                        "runtime.shutdown.requested",
                        "Runtime shutdown requested; draining durable work before disconnect",
                    )
                else:
                    await telegram_task
            finally:
                for task in (lease_lost_task, shutdown_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(
                    lease_lost_task,
                    shutdown_task,
                    return_exceptions=True,
                )
                for sig in registered_signals:
                    loop.remove_signal_handler(sig)
        finally:
            if alert_runtime is not None:
                await alert_runtime.stop()
            if maintenance_runtime is not None:
                await maintenance_runtime.stop()
            if notification_runtime is not None:
                await notification_runtime.stop()
            if dashboard_server is not None:
                await dashboard_server.stop()
            await auto_recovery_runtime.stop()
            await intake_runtime.stop()
            await bot_ui.stop()
            if archive_runtime is not None:
                await archive_runtime.stop()
            await cache_runtime.stop()
            await runtime_health.stop()
            await gateway.stop()
            if telegram_task is not None:
                await asyncio.gather(telegram_task, return_exceptions=True)
    finally:
        if runtime_lease is not None:
            await runtime_lease.stop()
        await repository.close()


def _disk_free_bytes(path: Path) -> int | None:
    snapshot = _disk_snapshot(path)
    return None if snapshot is None else int(snapshot["free_bytes"])


def _disk_snapshot(path: Path) -> dict[str, int] | None:
    try:
        usage = shutil.disk_usage(path if path.exists() else path.parent)
    except OSError:
        return None
    return {
        "total_bytes": int(usage.total),
        "used_bytes": int(usage.used),
        "free_bytes": int(usage.free),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="validate foundation without starting Telegram")
    args = parser.parse_args()
    asyncio.run(run(check_only=args.check))


if __name__ == "__main__":
    main()
