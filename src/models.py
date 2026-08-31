"""Domain objects shared by the intake, queue and transport layers."""

import time
from dataclasses import dataclass


@dataclass
class Job:
    seq: int
    kind: str
    status: object
    message: object = None
    album: list = None
    url: str = ""
    spoiler: bool = False
    user_id: int = 0
    cached_path: str = ""
    cleanup_extra: str = ""
    started: bool = False
    texts: list = None
    destination_profile_id: int | None = None
    destination_profile_snapshot: dict | None = None


@dataclass
class RetryInfo:
    job: object
    path: str = ""


@dataclass
class PendingJob:
    seq: int
    kind: str
    message: object = None
    album: list = None
    status: object = None
    timeout_task: object = None
    user_id: int = 0
    texts: list = None
    destination_profile_id: int | None = None
    destination_profile_name: str = ""
    destination_profile_snapshot: dict | None = None


@dataclass
class AlbumBuffer:
    chat_id: int
    messages: list
    grouped_ids: set
    task: object = None


@dataclass
class Session:
    user_id: int
    items: list = None
    status: object = None
    button_task: object = None
    started_at: float = 0.0
    texts: list = None

    def __post_init__(self) -> None:
        if self.items is None:
            self.items = []
        if self.texts is None:
            self.texts = []
        if not self.started_at:
            self.started_at = time.time()

    @property
    def media_count(self) -> int:
        return sum(len(item) for item in self.items)

    @property
    def text_count(self) -> int:
        return len(self.texts)
