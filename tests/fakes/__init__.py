"""Reusable offline fakes for bot orchestration tests."""

from .telegram import (
    FakeCallbackEvent,
    FakeClient,
    FakeDownloader,
    FakeMessage,
    FakeNewMessageEvent,
    FakePublisher,
    FakeStatusMessage,
)
from .webdav import FakeBackupClient
from .clock import FakeClock

__all__ = [
    "FakeCallbackEvent",
    "FakeClient",
    "FakeDownloader",
    "FakeMessage",
    "FakeNewMessageEvent",
    "FakePublisher",
    "FakeStatusMessage",
    "FakeBackupClient",
    "FakeClock",
]
