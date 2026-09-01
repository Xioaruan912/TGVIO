import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.dashboard import DashboardServer
from src.repository import DestinationProfileRecord, SQLiteRepository
from src.services.dashboard import DashboardService
from src.services.stats import RuntimeStatsSnapshot


def _snapshot() -> RuntimeStatsSnapshot:
    return RuntimeStatsSnapshot(
        uptime_seconds=123.0,
        today_accepted=4,
        today_succeeded=2,
        today_failed=1,
        today_cancelled=1,
        total_published_bytes=1000,
        total_backed_up_bytes=800,
        saved_upload_bytes=200,
        running=1,
        waiting=2,
        failed=1,
        cpu_percent=2.5,
        memory_mb=64.0,
        disk_used_bytes=2000,
        disk_total_bytes=10000,
        disk_free_bytes=8000,
        disk_reserved_bytes=100,
        protected_cache_bytes=50,
        reclaimable_cache_bytes=25,
        disk_enforce=True,
        disk_healthy=True,
        telegram_connected=True,
        webdav_enabled=True,
        webdav_health="正常<script>",
        webdav_age_seconds=5.0,
        database_ok=True,
        recent_errors=(("network_timeout<script>", 1),),
        recent_events=(("published", 2),),
    )


class _Stats:
    def __init__(self) -> None:
        self.calls = 0

    async def snapshot(self):
        self.calls += 1
        return _snapshot()

    @staticmethod
    def heartbeat_snapshot():
        return True, True, 1.25


class _Destinations:
    async def list_profiles(self):
        return [
            DestinationProfileRecord(
                id=1,
                name="默认 <目标>",
                destination_peer="@private_destination",
                discussion_group_peer="-100987654321",
                channel_at="@private_destination",
                group_at="@private_group",
                cover_mode=True,
                forward_caption=False,
                default_spoiler_mode="always_normal",
                backup_policy="best_effort",
                footer_template="private footer",
                enabled=True,
                is_default=True,
                read_only=True,
                source_kind="env",
                verified_at=1.0,
                created_at=1.0,
                updated_at=1.0,
            )
        ]


class DashboardReadModelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        downloads = root / "downloads"
        downloads.mkdir()
        self.repo = SQLiteRepository(root / "state.sqlite3", download_root=downloads)
        await self.repo.open()
        await self.repo.migrate()
        pipeline = SimpleNamespace(
            disk=None,
            destination_profiles=_Destinations(),
        )
        self.stats = _Stats()
        self.service = DashboardService(pipeline, self.repo, self.stats)

    async def asyncTearDown(self) -> None:
        await self.repo.close()

    async def test_jobs_are_paginated_and_strip_private_content(self) -> None:
        await self.repo.accept_job(
            kind="url",
            user_id=99887766,
            state="queued",
            source_kind="url",
            source_url="https://user:password@example.invalid/private-video",
            texts=["private caption"],
            items=[
                {
                    "original_name": "private-name.mp4",
                    "media_kind": "video",
                    "size_bytes": 123,
                }
            ],
        )
        dto = await self.service.jobs(page_size=10)
        self.assertEqual(dto["schema_version"], 1)
        self.assertEqual(dto["total"], 1)
        self.assertEqual(dto["items"][0]["title"], "链接任务")
        rendered = json.dumps(dto, ensure_ascii=False)
        for secret in (
            "99887766",
            "password",
            "example.invalid",
            "private caption",
            "private-name.mp4",
        ):
            self.assertNotIn(secret, rendered)

    async def test_routing_omits_peers_owners_and_footer(self) -> None:
        dto = await self.service.routing()
        rendered = json.dumps(dto, ensure_ascii=False)
        self.assertIn("默认 ‹目标›", rendered)
        self.assertEqual(dto["sources"], [])
        for secret in (
            "private_destination",
            "private_group",
            "99887766",
            "private footer",
        ):
            self.assertNotIn(secret, rendered)

    async def test_metrics_use_only_bounded_labels(self) -> None:
        metrics = await self.service.metrics()
        self.assertIn("tvf_up 1", metrics)
        self.assertIn('tvf_notification_outbox{state="pending"}', metrics)
        self.assertNotIn("network_timeout", metrics)
        self.assertNotIn("job_id", metrics)
        self.assertNotIn("user_id", metrics)
        self.assertNotIn("<script>", metrics)

    async def test_concurrent_endpoints_share_short_lived_runtime_snapshot(self) -> None:
        await asyncio.gather(
            self.service.overview(),
            self.service.storage(),
            self.service.health(),
            self.service.metrics(),
        )
        self.assertEqual(self.stats.calls, 1)


class _HttpService:
    async def overview(self):
        return {"schema_version": 1, "ok": True}

    async def jobs(self, **kwargs):
        return {"schema_version": 1, "query": kwargs, "items": []}

    async def routing(self):
        return {"schema_version": 1, "destinations": [], "sources": []}

    async def storage(self):
        return {"schema_version": 1, "disk": {}}

    async def health(self):
        return {"schema_version": 1, "live": True}

    async def metrics(self):
        return "tvf_up 1\n"


class DashboardHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.html = Path(self.tempdir.name) / "dashboard.html"
        self.html.write_text("<!doctype html><title>dashboard</title>", encoding="utf-8")
        self.token = "t" * 32
        self.server = DashboardServer(
            _HttpService(),
            token=self.token,
            host="127.0.0.1",
            port=0,
            socket_path="",
            html_path=self.html,
        )
        await self.server.start()
        self.port = int(self.server.sockets[0].getsockname()[1])

    async def asyncTearDown(self) -> None:
        await self.server.stop()

    async def _request(self, raw: bytes) -> bytes:
        reader, writer = await __import__("asyncio").open_connection("127.0.0.1", self.port)
        writer.write(raw)
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        return response

    async def test_html_is_local_shell_with_security_headers(self) -> None:
        response = await self._request(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        self.assertIn(b"200 OK", response)
        self.assertIn(b"Content-Security-Policy:", response)
        self.assertIn(b"X-Frame-Options: DENY", response)
        self.assertIn(b"Cache-Control: no-store", response)
        self.assertIn(b"<title>dashboard</title>", response)

    async def test_api_requires_bearer_and_rejects_query_credentials(self) -> None:
        unauthorized = await self._request(
            b"GET /api/v1/overview HTTP/1.1\r\nHost: localhost\r\n\r\n"
        )
        self.assertIn(b"401 Unauthorized", unauthorized)
        query_token = await self._request(
            b"GET /api/v1/overview?token=secret HTTP/1.1\r\nHost: localhost\r\n\r\n"
        )
        self.assertIn(b"400 Bad Request", query_token)
        authorized = await self._request(
            (
                "GET /api/v1/jobs?filter=failed&page=1&page_size=5 HTTP/1.1\r\n"
                f"Host: localhost\r\nAuthorization: Bearer {self.token}\r\n\r\n"
            ).encode()
        )
        self.assertIn(b"200 OK", authorized)
        body = authorized.split(b"\r\n\r\n", 1)[1]
        self.assertEqual(json.loads(body)["query"]["filter_name"], "failed")

    async def test_non_get_and_request_body_are_rejected(self) -> None:
        post = await self._request(b"POST /api/v1/overview HTTP/1.1\r\nHost: localhost\r\n\r\n")
        self.assertIn(b"405 Method Not Allowed", post)
        body = await self._request(
            b"GET / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 1\r\n\r\nx"
        )
        self.assertIn(b"400 Bad Request", body)

    async def test_metrics_are_authenticated_and_head_has_no_body(self) -> None:
        unauthorized = await self._request(
            b"GET /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n"
        )
        self.assertIn(b"401 Unauthorized", unauthorized)
        response = await self._request(
            (
                "HEAD /metrics HTTP/1.1\r\n"
                f"Host: localhost\r\nAuthorization: Bearer {self.token}\r\n\r\n"
            ).encode()
        )
        self.assertIn(b"200 OK", response)
        self.assertEqual(response.split(b"\r\n\r\n", 1)[1], b"")

    async def test_non_loopback_bind_requires_explicit_runtime_authorization(self) -> None:
        server = DashboardServer(
            _HttpService(), token=self.token, host="0.0.0.0", port=0,
            socket_path="", html_path=self.html,
        )
        with self.assertRaises(RuntimeError):
            await server.start()
        authorized = DashboardServer(
            _HttpService(), token=self.token, host="0.0.0.0", port=0,
            socket_path="", public_bind=True, html_path=self.html,
        )
        await authorized.start()
        try:
            self.assertTrue(authorized.serving)
        finally:
            await authorized.stop()


class DashboardAssetContractTests(unittest.TestCase):
    def test_demo_and_live_page_share_o1_v1_contract(self) -> None:
        html = (
            Path(__file__).resolve().parents[1] / "demo" / "o1-dashboard-taste.html"
        ).read_text(encoding="utf-8")
        for marker in (
            "dashboardMockDto",
            "schema_version: 1",
            "progress_percent",
            "notification_outbox",
            "'/api/v1/overview'",
            "'/api/v1/jobs?page=0&page_size=100'",
            "'/api/v1/storage'",
            "'/api/v1/routing'",
            "'/api/v1/health'",
        ):
            self.assertIn(marker, html)
        self.assertNotIn("来源媒体-4812.mp4", html)
        self.assertNotIn("公开示例视频", html)


class DashboardUnixSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_socket_is_private_and_removed_on_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            html = root / "dashboard.html"
            html.write_text("ok", encoding="utf-8")
            socket_path = root / "dashboard.sock"
            server = DashboardServer(
                _HttpService(),
                token="u" * 32,
                socket_path=str(socket_path),
                html_path=html,
            )
            await server.start()
            try:
                self.assertEqual(os.stat(socket_path).st_mode & 0o777, 0o600)
            finally:
                await server.stop()
            self.assertFalse(socket_path.exists())


if __name__ == "__main__":
    unittest.main()
