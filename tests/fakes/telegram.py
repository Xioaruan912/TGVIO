"""Small Telethon-shaped fakes that never perform network IO."""

import asyncio
from dataclasses import dataclass
from typing import Any


class FakeStatusMessage:
    _next_id = 1

    def __init__(self, text: str = "") -> None:
        self.id = self._next_id
        type(self)._next_id += 1
        self.text = text
        self.edits: list[dict[str, Any]] = []
        self.delete_calls = 0
        self.edit_exception: Exception | None = None
        self.delete_exception: Exception | None = None

    @property
    def deleted(self) -> bool:
        return self.delete_calls > 0

    async def edit(self, text: str, **kwargs: Any) -> "FakeStatusMessage":
        if self.edit_exception is not None:
            raise self.edit_exception
        self.text = text
        self.edits.append({"text": text, **kwargs})
        return self

    async def delete(self) -> None:
        if self.delete_exception is not None:
            raise self.delete_exception
        self.delete_calls += 1


@dataclass
class FakeMessage:
    id: int
    media: object = None
    grouped_id: int | None = None
    raw_text: str = ""


class FakeClient:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.sent_messages: list[dict[str, Any]] = []
        self.sent_files: list[dict[str, Any]] = []
        self.deleted_messages: list[tuple[object, object]] = []
        self.send_exception: Exception | None = None
        self.connected = True

    def on(self, _event_builder: object):
        def decorator(handler):
            self.handlers[handler.__name__] = handler
            return handler

        return decorator

    async def send_message(self, peer: object, text: str, **kwargs: Any):
        if self.send_exception is not None:
            raise self.send_exception
        message = FakeStatusMessage(text)
        self.sent_messages.append(
            {"peer": peer, "text": text, "message": message, **kwargs}
        )
        return message

    async def send_file(self, peer: object, file: object, **kwargs: Any):
        message = FakeStatusMessage()
        self.sent_files.append(
            {"peer": peer, "file": file, "message": message, **kwargs}
        )
        return message

    async def delete_messages(self, peer: object, message_ids: object) -> None:
        self.deleted_messages.append((peer, message_ids))

    async def disconnect(self) -> None:
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    def is_connected(self) -> bool:
        return bool(self.connected)


class FakeCallbackEvent:
    def __init__(
        self,
        client: FakeClient,
        data: bytes,
        sender_id: int = 42,
        chat_id: int | None = None,
    ) -> None:
        self.client = client
        self.data = data
        self.sender_id = sender_id
        self.chat_id = sender_id if chat_id is None else chat_id
        self.answers: list[str] = []
        self.edits: list[dict[str, Any]] = []
        self.responses: list[FakeStatusMessage] = []
        self.delete_calls = 0

    @property
    def is_private(self) -> bool:
        return self.chat_id == self.sender_id

    async def answer(self, text: str = "") -> None:
        self.answers.append(text)

    async def edit(self, text: str, **kwargs: Any) -> None:
        self.edits.append({"text": text, **kwargs})

    async def delete(self) -> None:
        self.delete_calls += 1

    async def respond(self, text: str, **kwargs: Any) -> FakeStatusMessage:
        message = FakeStatusMessage(text)
        self.responses.append(message)
        return message


class FakeNewMessageEvent:
    def __init__(
        self,
        client: FakeClient,
        raw_text: str,
        sender_id: int = 42,
        chat_id: int | None = None,
        message: FakeMessage | None = None,
    ) -> None:
        self.client = client
        self.raw_text = raw_text
        self.sender_id = sender_id
        self.chat_id = sender_id if chat_id is None else chat_id
        self.message = message or FakeMessage(1, raw_text=raw_text)
        self.responses: list[FakeStatusMessage] = []
        self.replies: list[FakeStatusMessage] = []

    @property
    def is_private(self) -> bool:
        return self.chat_id == self.sender_id

    async def respond(self, text: str, **kwargs: Any) -> FakeStatusMessage:
        message = await self.client.send_message(self.chat_id, text, **kwargs)
        self.responses.append(message)
        return message

    async def reply(self, text: str, **kwargs: Any) -> FakeStatusMessage:
        message = await self.client.send_message(self.chat_id, text, **kwargs)
        self.replies.append(message)
        return message


class FakeDownloader:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.pre_download_hooks: list[Any] = []
        self.progress_hooks: list[Any] = []
        self.status_hooks: list[Any] = []
        self.post_download_hooks: list[Any] = []
        self.calls: list[int] = []
        self.results: dict[int, Any] = {}
        self.failures: dict[int, Exception] = {}
        self.blockers: dict[int, asyncio.Event] = {}
        self.expected_calls = 0
        self.started = asyncio.Event()

    async def run(self, job: object):
        seq = job.seq
        self.calls.append(seq)
        if self.expected_calls and len(self.calls) >= self.expected_calls:
            self.started.set()
        blocker = self.blockers.get(seq)
        if blocker is not None:
            await blocker.wait()
        if seq in self.failures:
            raise self.failures[seq]
        return self.results.get(seq, f"/fake/job-{seq}/media.mp4")


class FakePublisher:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.pre_publish_hooks: list[Any] = []
        self.progress_hooks: list[Any] = []
        self.post_publish_hooks: list[Any] = []
        self.checkpoint_hooks: list[Any] = []
        self.calls: list[int] = []
        self.jobs: list[object] = []
        self.payloads: list[Any] = []
        self.failures: dict[int, Exception] = {}
        self.blockers: dict[int, asyncio.Event] = {}
        self.expected_calls = 0
        self.expected_starts = 0
        self.completed = asyncio.Event()
        self.started = asyncio.Event()

    async def publish(self, job: object, payload: Any) -> list[int]:
        seq = job.seq
        self.calls.append(seq)
        self.jobs.append(job)
        self.payloads.append(payload)
        if self.expected_starts and len(self.calls) >= self.expected_starts:
            self.started.set()
        blocker = self.blockers.get(seq)
        if blocker is not None:
            await blocker.wait()
        if seq in self.failures:
            raise self.failures[seq]
        if self.expected_calls and len(self.calls) >= self.expected_calls:
            self.completed.set()
        return [seq]
