from __future__ import annotations

import unittest

from tgvio.adapters.telegram.intake_runtime import TelethonIntakeRuntime


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


class JobNumberingTests(unittest.IsolatedAsyncioTestCase):
    async def test_business_day_number_wins_over_accepted_order(self) -> None:
        runtime = _host(_StubRepository(display_no=15, accepted_order=28))
        self.assertEqual(await runtime._display_number("job-1"), 15)

    async def test_accepted_order_is_the_fallback(self) -> None:
        runtime = _host(_StubRepository(display_no=None, accepted_order=28))
        self.assertEqual(await runtime._display_number("job-1"), 28)

    async def test_missing_numbers_stay_missing(self) -> None:
        runtime = _host(_StubRepository())
        self.assertIsNone(await runtime._display_number("job-1"))


if __name__ == "__main__":
    unittest.main()
