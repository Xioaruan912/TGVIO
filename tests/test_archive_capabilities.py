from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.archive_capabilities import (
    ARCHIVE_CAPABILITY_COMPONENT,
    ARCHIVE_PROBE_COMPONENT,
    get_archive_capability_status,
    record_archive_probe_failure,
    record_archive_probe_success,
)
from tgvio.domain.archive import ArchiveCapabilities
from tgvio.infrastructure.sqlite import SQLiteJobRepository


class ArchiveCapabilityStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.repo = SQLiteJobRepository(Path(self.tmp.name) / "state.sqlite3")
        await self.repo.open()

    async def asyncTearDown(self) -> None:
        await self.repo.close()
        self.tmp.cleanup()

    @staticmethod
    def _capabilities() -> ArchiveCapabilities:
        return ArchiveCapabilities(
            supports_propfind=True,
            supports_mkcol=True,
            supports_put=True,
            supports_move=True,
            supports_get=True,
            supports_etag=True,
            supports_quota=False,
        )

    async def test_success_is_durable_and_failure_does_not_overwrite_confirmed_capability(self) -> None:
        await record_archive_probe_success(
            self.repo,
            profile_id="primary",
            capabilities=self._capabilities(),
            now_epoch=1000,
        )
        await record_archive_probe_failure(
            self.repo,
            profile_id="primary",
            now_epoch=1100,
        )

        status = await get_archive_capability_status(
            self.repo,
            profile_id="primary",
            now_epoch=1100,
        )
        self.assertEqual(status.freshness, "fresh")
        self.assertEqual(status.last_probe_status, "unreachable")
        self.assertEqual(status.confirmed_at_epoch, 1000)
        self.assertEqual(status.expires_at_epoch, 4600)
        self.assertEqual(status.commit_mode, "move")
        self.assertTrue(status.supports_put)
        self.assertTrue(status.supports_etag)

        health = await self.repo.get_runtime_health()
        self.assertEqual(health[ARCHIVE_CAPABILITY_COMPONENT]["status"], "confirmed")
        self.assertEqual(health[ARCHIVE_PROBE_COMPONENT]["status"], "unreachable")
        self.assertEqual(
            health[ARCHIVE_CAPABILITY_COMPONENT]["detail"]["confirmed_at_epoch"],
            1000,
        )

    async def test_capability_has_explicit_stale_and_profile_mismatch_semantics(self) -> None:
        await record_archive_probe_success(
            self.repo,
            profile_id="primary",
            capabilities=self._capabilities(),
            now_epoch=1000,
        )
        stale = await get_archive_capability_status(
            self.repo,
            profile_id="primary",
            now_epoch=4601,
        )
        self.assertEqual(stale.freshness, "stale")
        self.assertEqual(stale.last_probe_status, "reachable")

        different = await get_archive_capability_status(
            self.repo,
            profile_id="other",
            now_epoch=1100,
        )
        self.assertEqual(different.freshness, "unknown")
        self.assertEqual(different.last_probe_status, "unknown")
        self.assertIsNone(different.commit_mode)


if __name__ == "__main__":
    unittest.main()
