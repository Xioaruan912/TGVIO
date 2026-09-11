from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.observability.logging import SafeJsonFormatter, configure_logging, log_event


class StructuredLoggingTests(unittest.TestCase):
    def test_formatter_redacts_urls_and_secret_assignments(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(SafeJsonFormatter())
        logger = logging.getLogger("tests.logging.redaction")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)

        log_event(
            logger,
            logging.INFO,
            "fixture.redaction",
            "download https://example.test/file?token=abc password=hunter2 "
            "/app/downloads/job-1/private-name.mp4",
            job_id="job-1",
            source_url="https://secret.example.test/signed?token=abc",
        )
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["event"], "fixture.redaction")
        self.assertEqual(payload["job_id"], "job-1")
        self.assertNotIn("source_url", payload)
        rendered = json.dumps(payload)
        self.assertNotIn("example.test", rendered)
        self.assertNotIn("hunter2", rendered)
        self.assertNotIn("token=abc", rendered)
        self.assertNotIn("private-name.mp4", rendered)

    def test_configure_logging_writes_jsonl_and_rotates_safely(self) -> None:
        with TemporaryDirectory() as tmp:
            configure_logging(
                level="INFO",
                log_dir=Path(tmp),
                file_enabled=True,
                max_bytes=1024 * 1024,
                backup_count=2,
            )
            logger = logging.getLogger("tests.logging.file")
            log_event(
                logger,
                logging.INFO,
                "fixture.file",
                job_id="job-2",
                item_count=3,
            )
            for handler in logging.getLogger().handlers:
                handler.flush()
            line = (Path(tmp) / "tgvio.jsonl").read_text(encoding="utf-8").strip()
            payload = json.loads(line)
            self.assertEqual(payload["event"], "fixture.file")
            self.assertEqual(payload["item_count"], 3)

