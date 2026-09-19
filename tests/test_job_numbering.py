from __future__ import annotations

import unittest

from tgvio.adapters.telegram.intake_runtime import TelethonIntakeRuntime
from tgvio.adapters.telegram.intake_status import IntakeStatusMixin
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind


class _StubRepository:
    def __init__(self, *, display_no: int | None = None, accepted_order: int | None = None) -> None:
        self._display_no = display_no
        self._accepted_order = accepted_order

    async def get_display_no(self, job_id: str) -> int | None:
        return self._display_no

    async def get_accepted_order(self, job_id: str) -> int | None:
        return self._accepted_order


def _host(repository) -> TelethonIntakeRuntime:
    runtime = TelethonIntakeRuntime.__new__(TelethonIntakeRuntime)
    runtime._repository = repository
    return runtime


def _job() -> Job:
    return Job(
        owner_id=7,
        destination="@channel",
        state=JobState.RECEIVED,
        items=[MediaItem(index=0, kind=MediaKind.VIDEO, source="fixture")],
    )


class JobNumberingTests(unittest.IsolatedAsyncioTestCase):
    async def test_business_day_number_wins_over_accepted_order(self) -> None:
        runtime = _host(_StubRepository(display_no=15, accepted_order=28))
        self.assertEqual(await runtime._job_label(_job()), "任务 #15")
        self.assertEqual(
            await runtime._accepted_status_text(_job()),
            "✅ 已接收 · **任务 #15**\n媒体：1\n正在下载和分析。",
        )

    async def test_accepted_order_is_the_fallback(self) -> None:
        runtime = _host(_StubRepository(display_no=None, accepted_order=28))
        self.assertEqual(await runtime._job_label(_job()), "任务 #28")

    async def test_missing_repository_numbers_degrade_to_a_plain_label(self) -> None:
        runtime = _host(_StubRepository())
        self.assertEqual(await runtime._job_label(_job()), "任务")

    async def test_number_accepts_a_job_id_too(self) -> None:
        runtime = _host(_StubRepository(display_no=9))
        self.assertEqual(await runtime._display_number("job-1"), 9)


class LiveStatusNumberingTests(unittest.TestCase):
    def test_live_status_uses_the_supplied_number(self) -> None:
        class _UI(IntakeStatusMixin):
            pass

        text = _UI()._render_live_status(_job(), None, None, accepted_order=15)
        self.assertIn("任务 #15", text)


if __name__ == "__main__":
    unittest.main()
