import asyncio
import unittest
from unittest.mock import patch

from src import main


class _FakeLoop:
    def __init__(self) -> None:
        self.callback = None
        self.tasks = []

    def add_signal_handler(self, _signal, callback) -> None:
        self.callback = callback

    def create_task(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task


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
        await asyncio.gather(*loop.tasks)
        self.assertEqual(client.disconnect_calls, 1)

