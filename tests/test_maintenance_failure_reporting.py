from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from test_rendition_sidecars import fixture
from tgvio.adapters.webdav_archive_support import WebDavArchiveError
from tgvio.infrastructure.rendition_state import MaintenanceState
from tgvio.observability.logging import maintenance_failure_fields


class FailurePrivacyTests(unittest.TestCase):
    def test_unknown_exception_content_is_not_reported(self):
        sensitive = "fixture-private https://fixture.invalid/private password=fixture-secret"
        error = RuntimeError(sensitive)
        error.status = "fixture-secret"
        fields = maintenance_failure_fields(error)
        self.assertEqual(fields, {"error": "RuntimeError", "failure_code": "unclassified"})
        self.assertNotIn("fixture", json.dumps(fields))

    def test_known_archive_failure_has_only_category_and_valid_http_status(self):
        fields = maintenance_failure_fields(
            WebDavArchiveError("WebDAV metadata PUT failed", status=503))
        self.assertEqual(fields, {"error": "WebDavArchiveError",
                                 "failure_code": "archive_metadata_put", "http_status": 503})
        invalid = WebDavArchiveError("WebDAV metadata PUT failed", status=True)
        self.assertNotIn("http_status", maintenance_failure_fields(invalid))


    def test_wrapped_upload_error_exposes_only_allowlisted_cause_type(self):
        error = WebDavArchiveError("WebDAV archive PUT result was not verifiable")
        error.__cause__ = TimeoutError("fixture-private password=secret")
        self.assertEqual(maintenance_failure_fields(error)["cause_error"], "TimeoutError")
        self.assertNotIn("fixture", json.dumps(maintenance_failure_fields(error)))
        error.__cause__ = type("fixture_private", (RuntimeError,), {})("fixture-private")
        self.assertNotIn("cause_error", maintenance_failure_fields(error))


class WorkerFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_reports_failure_phase_without_resetting_checkpoint(self):
        from tgvio.interfaces.backfill_renditions import work

        _, task = fixture()
        task = replace(task, media={**task.media, "size_bytes": 1024})
        with tempfile.TemporaryDirectory() as tmp:
            state = MaintenanceState(Path(tmp) / "progress.sqlite3", persistent_retry=True)
            state.discover([task])
            with patch("tgvio.infrastructure.rendition_state.time.time", return_value=1):
                for _ in range(5):
                    state.fail(task, TimeoutError())
            state.conn.close()
            args = SimpleNamespace(work_dir=tmp, status=False, retry_failed=False,
                                   watch=False, dry_run=False, limit=1)
            discovery = SimpleNamespace(tasks=AsyncMock(return_value=[task]))
            runner = SimpleNamespace(run=AsyncMock(side_effect=WebDavArchiveError(
                "WebDAV archive PUT failed size verification", status=201)))
            with patch.dict("os.environ", {
                "TGVIO_ARCHIVE_WEBDAV_URL": "https://fixture.invalid",
                "TGVIO_ARCHIVE_WEBDAV_USER": "fixture",
                "TGVIO_ARCHIVE_WEBDAV_PASSWORD": "fixture"}), \
                 patch("tgvio.interfaces.backfill_renditions.RenditionArchivePort"), \
                 patch("tgvio.interfaces.backfill_renditions.RenditionDiscovery",
                       return_value=discovery), \
                 patch("tgvio.interfaces.backfill_renditions.RenditionBackfill",
                       return_value=runner), \
                 patch("tgvio.interfaces.backfill_renditions.report") as report, \
                 patch("tgvio.interfaces.backfill_renditions.shutil.disk_usage",
                       return_value=SimpleNamespace(free=3*1024**3)), \
                 patch("tgvio.interfaces.backfill_renditions.asyncio.sleep", new=AsyncMock()):
                await work(args)
            self.assertEqual(runner.run.await_count, 1, [c.args[0] for c in report.call_args_list])
            failure = next(c.kwargs for c in report.call_args_list if c.args[0] == "failed")
            self.assertEqual(failure["failure_code"], "archive_upload_size")
            self.assertEqual(failure["http_status"], 201)
            self.assertEqual(failure["error"], "WebDavArchiveError")
            state = MaintenanceState(Path(tmp) / "progress.sqlite3", persistent_retry=True)
            row = state.conn.execute("SELECT status,attempts,error FROM tasks").fetchone()
            self.assertEqual(row, ("failed", 6, "WebDavArchiveError"))
            self.assertFalse(state.eligible(task))
            state.conn.close()
