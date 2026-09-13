from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from tgvio.adapters.web.dashboard import DashboardServer
from tgvio.application.dashboard import DashboardService
from tgvio.application.metrics import MetricsService
from tgvio.domain.diagnostics import (
    ArchiveDiagnostic,
    DiagnosticAvailability,
    DiagnosticSnapshot,
    FeatureDiagnostics,
    LeaseFreshness,
    MigrationVerification,
    RuntimeLeaseDiagnostic,
    SchedulerDiagnostic,
    SchemaDiagnostic,
    StaticProxyDiagnostic,
    StaticProxyState,
)
from tgvio.infrastructure.sqlite import SQLiteJobRepository


def _snapshot() -> DiagnosticSnapshot:
    return DiagnosticSnapshot(
        release_id="r2-09-test",
        app_version="0.0.0",
        commit="a" * 40,
        source_manifest="b" * 64,
        aggregate_status=DiagnosticAvailability.READY,
        schema=SchemaDiagnostic(8, 8, True, MigrationVerification.VERIFIED),
        runtime_lease=RuntimeLeaseDiagnostic(True, 3, LeaseFreshness.FRESH),
        scheduler=SchedulerDiagnostic(False, 1, 0, 0, 0),
        archive=ArchiveDiagnostic(0, 0, 1, 0, 0, "fresh"),
        features=FeatureDiagnostics(
            run_bot=True,
            publish_enabled=True,
            url_enabled=False,
            url_private_network_policy="block",
            archive_enabled=True,
            archive_policy="required",
            collections_enabled=True,
            auto_retry_enabled=True,
            live_fixture_enabled=False,
        ),
        static_proxy=StaticProxyDiagnostic(StaticProxyState.DISABLED, None),
    )


class _StubDiagnostics:
    async def snapshot(self) -> DiagnosticSnapshot:
        return _snapshot()


class _StubMetricsDiagnostics(_StubDiagnostics):
    pass


class DashboardServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()
        self.service = DashboardService(
            self.repo,
            _StubDiagnostics(),  # type: ignore[arg-type]
            disk_usage=lambda: {"total_bytes": 100, "used_bytes": 40, "free_bytes": 60},
        )

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    async def _insert_job(self, job_id: str, state: str) -> None:
        conn = self.repo._require()
        await conn.execute(
            "INSERT INTO jobs(id, owner_id, destination, state, policy_json, error_code) "
            "VALUES(?, 7, '@channel', ?, ?, 'x')",
            (job_id, state, "{}"),
        )
        await conn.execute("INSERT INTO job_schedule(job_id) VALUES(?)", (job_id,))
        await conn.execute(
            "INSERT INTO job_items(job_id, item_index, kind, source, caption, local_path, size_bytes) "
            "VALUES(?, 0, 'video', 'telegram', 'secret caption', '/root/private/media.bin', 1234)",
            (job_id,),
        )
        await conn.commit()

    async def test_overview_is_redacted_and_read_only(self) -> None:
        await self._insert_job("job-a", "succeeded")
        overview = await self.service.overview()
        text = repr(overview)
        for forbidden in ("secret caption", "/root/private", "media.bin", "@channel", "'7'", ": 7"):
            self.assertNotIn(forbidden, text)
        assert isinstance(overview["release"], dict)
        self.assertEqual(overview["release"]["commit"], "a" * 40)
        assert isinstance(overview["jobs"], dict)
        self.assertEqual(overview["jobs"].get("succeeded"), 1)

    async def test_jobs_page_is_bounded_and_redacted(self) -> None:
        for index in range(3):
            await self._insert_job(f"job-{index}", "succeeded")
        page = await self.service.jobs(filter="completed", page=0, page_size=2)
        self.assertEqual(page["total"], 3)
        self.assertEqual(len(page["entries"]), 2)
        entry_text = repr(page["entries"])
        self.assertNotIn("secret caption", entry_text)
        self.assertNotIn("/root/private", entry_text)
        self.assertNotIn("owner_id", entry_text)
        for entry in page["entries"]:
            self.assertEqual(
                set(entry),
                {
                    "job_id",
                    "state",
                    "error_code",
                    "updated_at",
                    "media_count",
                    "bytes",
                    "accepted_order",
                },
            )


class DashboardHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()
        service = DashboardService(self.repo, _StubDiagnostics())  # type: ignore[arg-type]
        self.server = DashboardServer(
            service,
            MetricsService(self.repo, _StubMetricsDiagnostics()),  # type: ignore[arg-type]
            host="127.0.0.1",
            port=0,
            token="a" * 40,
        )
        await self.server.start()
        self.port = self.server.bound_port

    async def asyncTearDown(self) -> None:
        await self.server.stop()
        await self.repo.close()
        self.tmp.cleanup()

    async def _request(self, raw: bytes) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(raw)
        await writer.drain()
        data = await reader.read()
        writer.close()
        await writer.wait_closed()
        return data

    def _get(self, path: str, *, token: str | None = None, method: str = "GET") -> bytes:
        auth = f"Authorization: Bearer {token}\r\n" if token is not None else ""
        return f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n{auth}\r\n".encode()

    async def test_anonymous_data_and_metrics_are_unauthorized(self) -> None:
        response = await self._request(self._get("/api/v1/overview"))
        self.assertIn(b"401", response.split(b"\r\n", 1)[0])
        response = await self._request(self._get("/metrics"))
        self.assertIn(b"401", response.split(b"\r\n", 1)[0])

    async def test_bearer_authorizes_reads_with_security_headers(self) -> None:
        response = await self._request(self._get("/api/v1/overview", token="a" * 40))
        head, _, body = response.partition(b"\r\n\r\n")
        self.assertIn(b"200", head.split(b"\r\n", 1)[0])
        self.assertIn(b"X-Content-Type-Options: nosniff", head)
        self.assertIn(b"Content-Security-Policy", head)
        self.assertIn(b"release", body)
        metrics = await self._request(self._get("/metrics", token="a" * 40))
        self.assertIn(b"tgvio_jobs", metrics)
        self.assertIn(b"tgvio_notification_outbox", metrics)

    async def test_query_token_and_methods_are_rejected(self) -> None:
        response = await self._request(self._get("/api/v1/overview?token=abc"))
        self.assertIn(b"400", response.split(b"\r\n", 1)[0])
        response = await self._request(self._get("/api/v1/overview", token="a" * 40, method="POST"))
        self.assertIn(b"405", response.split(b"\r\n", 1)[0])

    async def test_head_has_no_body_and_index_is_anonymous(self) -> None:
        response = await self._request(self._get("/api/v1/health", token="a" * 40, method="HEAD"))
        head, _, body = response.partition(b"\r\n\r\n")
        self.assertIn(b"200", head.split(b"\r\n", 1)[0])
        self.assertEqual(body, b"")
        shell = await self._request(self._get("/"))
        self.assertIn(b"200", shell.split(b"\r\n", 1)[0])
        self.assertIn(b"read-only", shell)
