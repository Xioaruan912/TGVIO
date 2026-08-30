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

__all__ = [
    "FakeCallbackEvent",
    "FakeClient",
    "FakeDownloader",
    "FakeMessage",
    "FakeNewMessageEvent",
    "FakePublisher",
    "FakeStatusMessage",
]
