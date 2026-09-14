from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tgvio.application.diagnostics import DiagnosticFeatureConfig, DiagnosticSnapshotService
from tgvio.domain.diagnostics import (
    CapabilitiesDiagnostic,
    DiagnosticAvailability,
    LeaseFreshness,
    MigrationVerification,
    StaticProxyState,
)
from tgvio.infrastructure.capabilities import probe_environment_capabilities
from tgvio.infrastructure.proxy_probe import probe_static_proxy_endpoint
from tgvio.infrastructure.sqlite import SQLiteJobRepository
from tgvio.main import run


class DiagnosticSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def test_snapshot_uses_sql_aggregates_for_1000_plus_jobs_and_redacts_health_detail(self) -> None:
        conn = self.repo._require()
        rows = [
            (f"bulk-{index:04d}", 42, "@channel", "succeeded", "{}", None)
            for index in range(1100)
        ]
        rows.extend(
            [
                ("active-1", 42, "@channel", "received", "{}", None),
                ("ready-1", 42, "@channel", "planned", "{}", None),
                ("ready-2", 42, "@channel", "publishing", "{}", None),
                ("held-1", 42, "@channel", "received", "{}", None),
                ("blocked-1", 42, "@channel", "failed", "{}", "publish_partial"),
                (
                    "blocked-2",
                    42,
                    "@channel",
                    "failed",
                    '{"auto_recovery_job":{"status":"quarantined"}}',
                    "download_failed",
                ),
                (
                    "archive-retry",
                    42,
                    "@channel",
                    "succeeded",
                    '{"auto_recovery_archive":{"status":"scheduled"}}',
                    None,
                ),
                ("archive-failed", 42, "@channel", "succeeded", "{}", None),
                ("archive-plan", 42, "@channel", "succeeded", "{}", None),
                ("archive-stage", 42, "@channel", "succeeded", "{}", None),
                ("archive-upload", 42, "@channel", "succeeded", "{}", None),
                ("archive-verify", 42, "@channel", "succeeded", "{}", None),
                ("archive-commit", 42, "@channel", "succeeded", "{}", None),
            ]
        )
        await conn.executemany(
            """
            INSERT INTO jobs(id, owner_id, destination, state, policy_json, error_code)
            VALUES(?,?,?,?,?,?)
            """,
            rows,
        )
        await conn.execute("INSERT INTO job_controls(job_id, hold_requested) VALUES('held-1',1)")
        archive_rows = [
            ("pkg-plan", "archive-plan", "planned"),
            ("pkg-stage", "archive-stage", "staging"),
            ("pkg-upload", "archive-upload", "uploading"),
            ("pkg-verify", "archive-verify", "verifying"),
            ("pkg-commit", "archive-commit", "committed"),
            ("pkg-retry", "archive-retry", "failed"),
            ("pkg-failed", "archive-failed", "failed"),
        ]
        await conn.executemany(
            """
            INSERT INTO archive_packages(
                id, job_id, layout_version, remote_path, staging_path, state, manifest_json
            ) VALUES(?,?, 'tgvio.archive/v1', 'redacted', 'redacted', ?, '{}')
            """,
            archive_rows,
        )
        await conn.commit()
        lease = await self.repo.acquire_runtime_lease(
            "telegram-runtime",
            "holder-must-never-appear",
            ttl_seconds=30,
        )
        self.assertIsNotNone(lease)
        await self.repo.set_runtime_health(
            "archive_capability",
            "confirmed",
            detail={
                "profile_id": "primary",
                "confirmed_at_epoch": 900,
                "expires_at_epoch": 2000,
            },
        )
        await self.repo.set_runtime_health(
            "static_proxy",
            "reachable",
            detail={
                "checked_at_epoch": 950,
                "url": "http://user:super-secret@proxy.example.invalid:8080",
                "path": "/root/private/session",
            },
        )

        service = DiagnosticSnapshotService(
            self.repo,
            features=DiagnosticFeatureConfig(
                run_bot=True,
                publish_enabled=True,
                url_enabled=True,
                url_private_network_policy="block",
                archive_enabled=True,
                archive_profile_id="primary",
                archive_policy="required",
                collections_enabled=True,
                auto_retry_enabled=True,
                live_fixture_enabled=False,
                static_proxy_configured=True,
            ),
            schema_status=self.repo.schema_status,
            now=lambda: 1000,
        )
        with patch.dict(
            os.environ,
            {
                "RELEASE_ID": "r2-07d-abcdef0-20260913T090000Z",
                "APP_COMMIT": "a" * 40,
                "SOURCE_MANIFEST": "b" * 64,
            },
            clear=False,
        ):
            snapshot = await service.snapshot()

        self.assertEqual(snapshot.aggregate_status, DiagnosticAvailability.READY)
        self.assertEqual(snapshot.schema.user_version, 10)
        self.assertEqual(snapshot.schema.latest_version, 10)
        self.assertTrue(snapshot.schema.ledger_contiguous)
        self.assertEqual(snapshot.schema.verification, MigrationVerification.VERIFIED)
        self.assertTrue(snapshot.runtime_lease.unique)
        self.assertEqual(snapshot.runtime_lease.freshness, LeaseFreshness.FRESH)
        self.assertEqual(snapshot.scheduler.active, 3)
        self.assertEqual(snapshot.scheduler.held, 1)
        self.assertEqual(snapshot.scheduler.ready, 2)
        self.assertEqual(snapshot.scheduler.blocked, 2)
        self.assertEqual(snapshot.archive.planned, 1)
        self.assertEqual(snapshot.archive.transferring, 3)
        self.assertEqual(snapshot.archive.committed, 1)
        self.assertEqual(snapshot.archive.failed, 2)
        self.assertEqual(snapshot.archive.retry_wait, 1)
        self.assertEqual(snapshot.archive.capability_freshness, "fresh")
        self.assertEqual(snapshot.static_proxy.state, StaticProxyState.REACHABLE)
        self.assertEqual(snapshot.static_proxy.checked_at_epoch, 950)
        projection = repr(snapshot)
        for forbidden in (
            "holder-must-never-appear",
            "super-secret",
            "proxy.example.invalid",
            "/root/private/session",
            "@channel",
        ):
            self.assertNotIn(forbidden, projection)

    async def test_snapshot_normalizes_failures_and_untrusted_release_environment(self) -> None:
        class FailingRepository:
            async def get_diagnostic_aggregates(self):
                raise RuntimeError("secret https://user:pw@example.invalid /root/private")

            async def get_runtime_health(self):
                raise RuntimeError("token=secret")

        service = DiagnosticSnapshotService(
            FailingRepository(),  # type: ignore[arg-type]
            features=DiagnosticFeatureConfig(
                run_bot=True,
                publish_enabled=False,
                url_enabled=False,
                url_private_network_policy="block",
                archive_enabled=False,
                archive_profile_id="primary",
                archive_policy="required",
                collections_enabled=True,
                auto_retry_enabled=True,
                live_fixture_enabled=False,
                static_proxy_configured=True,
            ),
            schema_status=lambda: (_ for _ in ()).throw(RuntimeError("/root/state.sqlite3")),
        )
        with patch.dict(
            os.environ,
            {
                "RELEASE_ID": "../../secret release",
                "APP_COMMIT": "token-value",
                "SOURCE_MANIFEST": "/root/private/source",
            },
            clear=False,
        ):
            snapshot = await service.snapshot()

        self.assertEqual(snapshot.aggregate_status, DiagnosticAvailability.UNAVAILABLE)
        self.assertEqual(snapshot.schema.verification, MigrationVerification.UNAVAILABLE)
        self.assertEqual(snapshot.release_id, "unknown")
        self.assertEqual(snapshot.commit, "unknown")
        self.assertEqual(snapshot.source_manifest, "unknown")
        self.assertEqual(snapshot.static_proxy.state, StaticProxyState.CONFIGURED_UNCHECKED)
        projection = repr(snapshot)
        for forbidden in ("secret", "example.invalid", "/root", "token-value"):
            self.assertNotIn(forbidden, projection)

    async def test_snapshot_bounds_untrusted_proxy_timestamp(self) -> None:
        await self.repo.set_runtime_health(
            "static_proxy",
            "reachable",
            detail={"checked_at_epoch": 10**100},
        )
        service = DiagnosticSnapshotService(
            self.repo,
            features=DiagnosticFeatureConfig(
                run_bot=True,
                publish_enabled=False,
                url_enabled=False,
                url_private_network_policy="block",
                archive_enabled=False,
                archive_profile_id="primary",
                archive_policy="required",
                collections_enabled=True,
                auto_retry_enabled=True,
                live_fixture_enabled=False,
                static_proxy_configured=True,
            ),
            schema_status=self.repo.schema_status,
        )

        snapshot = await service.snapshot()

        self.assertEqual(snapshot.static_proxy.state, StaticProxyState.REACHABLE)
        self.assertIsNone(snapshot.static_proxy.checked_at_epoch)


class StaticProxyProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_proxy_never_opens_a_connection(self) -> None:
        with patch(
            "tgvio.infrastructure.proxy_probe.asyncio.open_connection",
            new=AsyncMock(side_effect=AssertionError("must not connect")),
        ):
            result = await probe_static_proxy_endpoint("")
        self.assertEqual(result.state, StaticProxyState.DISABLED)
        self.assertIsNone(result.checked_at_epoch)

    async def test_proxy_probe_keeps_endpoint_and_credentials_out_of_result(self) -> None:
        writer = MagicMock()
        writer.wait_closed = AsyncMock()
        open_connection = AsyncMock(return_value=(object(), writer))
        with patch(
            "tgvio.infrastructure.proxy_probe.asyncio.open_connection",
            new=open_connection,
        ):
            result = await probe_static_proxy_endpoint(
                "http://user:super-secret@proxy.example.invalid:8080",
                timeout_seconds=1,
                now=lambda: 1234.0,
            )
        open_connection.assert_awaited_once_with("proxy.example.invalid", 8080)
        writer.close.assert_called_once_with()
        writer.wait_closed.assert_awaited_once_with()
        self.assertEqual(result.state, StaticProxyState.REACHABLE)
        self.assertEqual(result.checked_at_epoch, 1234)
        self.assertNotIn("super-secret", repr(result))
        self.assertNotIn("proxy.example.invalid", repr(result))

    async def test_proxy_probe_timeout_is_normalized_to_unreachable(self) -> None:
        with patch(
            "tgvio.infrastructure.proxy_probe.asyncio.open_connection",
            new=AsyncMock(side_effect=TimeoutError("credential-bearing failure")),
        ):
            result = await probe_static_proxy_endpoint(
                "socks5://user:pw@proxy.example.invalid:1080",
                timeout_seconds=1,
                now=lambda: 4321.0,
            )
        self.assertEqual(result.state, StaticProxyState.UNREACHABLE)
        self.assertEqual(result.checked_at_epoch, 4321)
        self.assertNotIn("credential", repr(result))

    async def test_proxy_probe_unexpected_connect_or_close_error_never_escapes(self) -> None:
        with patch(
            "tgvio.infrastructure.proxy_probe.asyncio.open_connection",
            new=AsyncMock(side_effect=ValueError("proxy.example.invalid credential error")),
        ):
            failed_connect = await probe_static_proxy_endpoint(
                "http://user:pw@proxy.example.invalid:8080",
                timeout_seconds=1,
                now=lambda: 5678.0,
            )
        self.assertEqual(failed_connect.state, StaticProxyState.UNREACHABLE)
        self.assertEqual(failed_connect.checked_at_epoch, 5678)
        self.assertNotIn("proxy.example.invalid", repr(failed_connect))

        writer = MagicMock()
        writer.wait_closed = AsyncMock(side_effect=ValueError("close detail"))
        with patch(
            "tgvio.infrastructure.proxy_probe.asyncio.open_connection",
            new=AsyncMock(return_value=(object(), writer)),
        ):
            close_failure = await probe_static_proxy_endpoint(
                "socks5://proxy.example.invalid:1080",
                timeout_seconds=1,
                now=lambda: 5679.0,
            )
        self.assertEqual(close_failure.state, StaticProxyState.REACHABLE)
        self.assertEqual(close_failure.checked_at_epoch, 5679)


class CheckOnlyStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_only_never_probes_a_configured_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            environment = {
                "API_ID": "123456",
                "API_HASH": "hash-value",
                "BOT_TOKEN": "bot-token",
                "DEST_CHANNEL": "@destination",
                "ALLOWED_USERS": "42",
                "TGVIO_RUN_BOT": "false",
                "TGVIO_LOG_FILE_ENABLED": "false",
                "TGVIO_DATA_DIR": str(root / "data"),
                "TGVIO_DOWNLOAD_DIR": str(root / "downloads"),
                "TGVIO_LOG_DIR": str(root / "logs"),
                "TGVIO_STATIC_PROXY_URL": "http://user:secret@proxy.example.invalid:8080",
            }
            probe = AsyncMock(side_effect=AssertionError("check-only must not connect"))
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("tgvio.main.load_dotenv"),
                patch("tgvio.main.probe_static_proxy_endpoint", new=probe),
            ):
                await run(check_only=True)

            probe.assert_not_awaited()
            repository = SQLiteJobRepository(root / "data" / "state.sqlite3")
            await repository.open()
            try:
                health = await repository.get_runtime_health()
            finally:
                await repository.close()
            self.assertEqual(health["static_proxy"]["status"], "configured_unchecked")
            self.assertEqual(health["static_proxy"]["detail"], {"version": 1})


class CapabilityDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def test_probe_reports_boolean_capabilities(self) -> None:
        caps = probe_environment_capabilities()
        for value in (caps.ffmpeg, caps.ffprobe, caps.yt_dlp, caps.cryptg, caps.hachoir):
            self.assertIsInstance(value, bool)

    async def test_snapshot_includes_capabilities_when_provider_given(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteJobRepository(Path(tmp) / "state.sqlite3")
            await repository.open()
            try:
                service = DiagnosticSnapshotService(
                    repository,
                    features=DiagnosticFeatureConfig(
                        run_bot=True,
                        publish_enabled=True,
                        url_enabled=False,
                        url_private_network_policy="block",
                        archive_enabled=False,
                        archive_profile_id="primary",
                        archive_policy="required",
                        collections_enabled=True,
                        auto_retry_enabled=True,
                        live_fixture_enabled=False,
                        static_proxy_configured=False,
                    ),
                    schema_status=repository.schema_status,
                    capabilities=lambda: CapabilitiesDiagnostic(
                        ffmpeg=True,
                        ffprobe=True,
                        yt_dlp=True,
                        cryptg=True,
                        hachoir=False,
                    ),
                )
                snapshot = await service.snapshot()
                self.assertIsNotNone(snapshot.capabilities)
                assert snapshot.capabilities is not None
                self.assertTrue(snapshot.capabilities.ffprobe)
                self.assertFalse(snapshot.capabilities.hachoir)
            finally:
                await repository.close()
