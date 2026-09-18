from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from tgvio.adapters.telegram.source_runtime import SourceCoordinator, SourceLoginError
from tgvio.application.runtime_flags import RuntimeFlags


class FakeRepository:
    def __init__(self) -> None:
        self.flags: dict[str, str] = {}

    async def set_runtime_flag(self, key: str, value: str) -> None:
        self.flags[str(key)] = str(value)


def _settings(**overrides):
    values = {
        "api_id": 123,
        "api_hash": "hash",
        "source_session": None,
        "source_chats": ("@env",),
        "source_trigger": "#env",
        "source_delete_trigger": True,
        "source_download_workers": 4,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class SourceCoordinatorConfigTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "source_user"
        self.repo = FakeRepository()
        self.flags = RuntimeFlags()

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def _coordinator(self, **overrides) -> SourceCoordinator:
        return SourceCoordinator(_settings(**overrides), self.repo, self.flags, self.path)

    async def test_defaults_come_from_settings(self) -> None:
        coordinator = self._coordinator()
        self.assertEqual(coordinator.effective_trigger(), "#env")
        self.assertEqual(coordinator.effective_chats(), ("@env",))
        self.assertTrue(coordinator.effective_delete_trigger())
        self.assertEqual(coordinator.status_line(), "未登录")

    async def test_runtime_flags_override_settings(self) -> None:
        coordinator = self._coordinator()
        await self.flags.set(self.repo, "source_trigger", "#custom")
        await self.flags.set(self.repo, "source_delete_trigger", "false")
        await self.flags.set(self.repo, "source_chats", json.dumps(["@a", "-1001"]))
        self.assertEqual(coordinator.effective_trigger(), "#custom")
        self.assertFalse(coordinator.effective_delete_trigger())
        self.assertEqual(coordinator.effective_chats(), ("@a", "-1001"))

    async def test_invalid_stored_whitelist_falls_back_to_settings(self) -> None:
        coordinator = self._coordinator()
        await self.flags.set(self.repo, "source_chats", "{not json")
        self.assertEqual(coordinator.effective_chats(), ("@env",))

    async def test_add_chat_requires_a_ready_client(self) -> None:
        coordinator = self._coordinator()
        with self.assertRaises(SourceLoginError):
            await coordinator.add_chat("@x")

    async def test_remove_chat_updates_persisted_whitelist(self) -> None:
        coordinator = self._coordinator()
        await self.flags.set(self.repo, "source_chats", json.dumps(["@a", "@b"]))
        message = await coordinator.remove_chat(0)
        self.assertIn("@a", message)
        self.assertEqual(coordinator.whitelist(), ["@b"])
        self.assertEqual(json.loads(self.repo.flags["source_chats"]), ["@b"])


if __name__ == "__main__":
    unittest.main()
