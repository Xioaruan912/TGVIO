import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src import main


class _FakeLoop:
    def __init__(self) -> None:
        self.callback = None

    def add_signal_handler(self, _signal, callback) -> None:
        self.callback = callback

class _FakeClient:
    def __init__(self) -> None:
        self.disconnect_calls = 0

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


class MainSignalTests(unittest.IsolatedAsyncioTestCase):
    async def test_sigterm_disconnects_client_once(self) -> None:
        loop = _FakeLoop()
        client = _FakeClient()
        with patch.object(main.asyncio, "get_running_loop", return_value=loop):
            main._install_sigterm_handler(client)
        self.assertIsNotNone(loop.callback)
        loop.callback()
        loop.callback()
        await asyncio.sleep(0)
        self.assertEqual(client.disconnect_calls, 1)


class _FakeDashboardService:
    def __init__(self, pipeline, repository, stats) -> None:
        self.values = (pipeline, repository, stats)


class _FakeDashboardServer:
    instances = []

    def __init__(self, service, **kwargs) -> None:
        self.service = service
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.__class__.instances.append(self)

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class _FakeNotifier:
    instances = []
    fail_start = False

    def __init__(self, repository, **kwargs) -> None:
        self.repository = repository
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.events = []
        self.__class__.instances.append(self)

    async def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("notifier start failed")
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def enqueue_runtime_event(self, event: str) -> None:
        self.events.append(event)


class MainO1LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _FakeDashboardServer.instances.clear()
        _FakeNotifier.instances.clear()
        _FakeNotifier.fail_start = False

    @staticmethod
    def _settings(**changes):
        values = dict(
            dashboard_enabled=True,
            dashboard_token="d" * 32,
            dashboard_host="127.0.0.1",
            dashboard_port=8787,
            dashboard_socket="session/dashboard.sock",
            webhook_enabled=True,
            webhook_url="https://hooks.example.invalid/tvf",
            webhook_token="w" * 32,
            webhook_timeout=10.0,
            webhook_max_attempts=8,
            webhook_poll_interval=2.0,
        )
        values.update(changes)
        return SimpleNamespace(**values)

    async def test_optional_services_start_and_stop_without_second_bot(self) -> None:
        pipeline = SimpleNamespace(stats_service=object())
        repository = object()
        with (
            patch.object(main, "DashboardService", _FakeDashboardService),
            patch.object(main, "DashboardServer", _FakeDashboardServer),
            patch.object(main, "WebhookNotifier", _FakeNotifier),
        ):
            dashboard, notifier = await main._start_o1_services(
                self._settings(), pipeline, repository
            )
            self.assertTrue(dashboard.started)
            self.assertTrue(notifier.started)
            self.assertEqual(notifier.events, ["started"])
            await main._stop_o1_services(dashboard, notifier)
        self.assertTrue(dashboard.stopped)
        self.assertTrue(notifier.stopped)

    async def test_partial_start_failure_stops_dashboard(self) -> None:
        _FakeNotifier.fail_start = True
        with (
            patch.object(main, "DashboardService", _FakeDashboardService),
            patch.object(main, "DashboardServer", _FakeDashboardServer),
            patch.object(main, "WebhookNotifier", _FakeNotifier),
        ):
            with self.assertRaises(RuntimeError):
                await main._start_o1_services(
                    self._settings(),
                    SimpleNamespace(stats_service=object()),
                    object(),
                )
        self.assertTrue(_FakeDashboardServer.instances[0].stopped)
        self.assertTrue(_FakeNotifier.instances[0].stopped)
