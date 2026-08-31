import asyncio
import unittest

from src.services.network import NetworkCoordinator


class NetworkCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_stale_failures_only_switch_once(self) -> None:
        current = {"value": -1}
        calls: list[int] = []

        async def apply(index: int) -> bool:
            calls.append(index)
            await asyncio.sleep(0)
            current["value"] = index
            return True

        coordinator = NetworkCoordinator(
            proxies=lambda: [{"url": "http://one"}, {"url": "http://two"}],
            current=lambda: current["value"],
            auto_enabled=lambda: True,
            apply_proxy=apply,
        )
        observed = coordinator.generation
        first, second = await asyncio.gather(
            coordinator.auto_switch(observed_generation=observed),
            coordinator.auto_switch(observed_generation=observed),
        )
        results = (first, second)
        self.assertEqual(calls, [0])
        self.assertEqual(coordinator.generation, 1)
        self.assertEqual(sum(1 for result in results if result.switched), 1)
        self.assertEqual(sum(1 for result in results if result.reevaluate), 1)

    async def test_manual_proxy_changes_are_serialized(self) -> None:
        current = {"value": -1}
        in_flight = 0
        max_in_flight = 0

        async def apply(index: int) -> bool:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.005)
            current["value"] = index
            in_flight -= 1
            return True

        coordinator = NetworkCoordinator(
            proxies=lambda: [{"url": "http://one"}, {"url": "http://two"}],
            current=lambda: current["value"],
            auto_enabled=lambda: True,
            apply_proxy=apply,
        )
        await asyncio.gather(coordinator.apply(0), coordinator.apply(1))
        self.assertEqual(max_in_flight, 1)
        self.assertEqual(coordinator.generation, 2)
        self.assertEqual(current["value"], 1)
