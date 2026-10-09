from __future__ import annotations

from collections import OrderedDict
import json
from pathlib import Path
import os
import shutil
import time


_LAYOUT = "layout.json"


class RangeStore:
    """Byte-bounded, chunk-granular, LRU disk cache for media byte ranges.

    Files live at ``<root>/<key>/<index>.bin`` and are only created once a whole
    chunk has been fetched, so a cached file is always complete. The whole store is
    bounded by ``max_bytes`` whatever the size of the library.

    Two tiers share that budget. The first ``head_chunks`` chunks of a clip are its
    *head* (what makes a clip start instantly); everything else is *playback* data.
    Heads may use at most ``head_share`` of the budget, so warming heads can never
    crowd out what is being watched, and watching can never wipe the warm heads.
    The tier follows from the chunk index alone, so it survives a restart without
    any extra metadata.
    """

    def __init__(
        self,
        root: Path,
        *,
        chunk_bytes: int,
        max_bytes: int,
        head_chunks: int = 0,
        head_share: float = 0.5,
    ) -> None:
        self._root = Path(root)
        self.chunk_bytes = max(64 * 1024, int(chunk_bytes))
        self.max_bytes = max(self.chunk_bytes, int(max_bytes))
        self.head_chunks = max(0, int(head_chunks))
        share = min(1.0, max(0.0, float(head_share)))
        # Never smaller than one head, or a freshly written head would evict itself.
        self.head_budget = (
            max(self.chunk_bytes * self.head_chunks, int(self.max_bytes * share)) if self.head_chunks else 0
        )
        self._index: "OrderedDict[tuple[str, int], int]" = OrderedDict()
        self._total = 0
        self._head_total = 0

    def is_head(self, index: int) -> bool:
        return index < self.head_chunks

    @property
    def head_bytes(self) -> int:
        return self._head_total

    def head_room(self) -> bool:
        """Whether one more head chunk fits inside the head budget."""
        return self.head_chunks > 0 and self._head_total + self.chunk_bytes <= self.head_budget

    @property
    def total_bytes(self) -> int:
        return self._total

    @property
    def chunk_count(self) -> int:
        return len(self._index)

    def open(self) -> None:
        self._index.clear()
        self._total = 0
        self._head_total = 0
        self._relayout()
        if not self._root.exists():
            return
        for media_dir in self._root.iterdir():
            if not media_dir.is_dir():
                continue
            key = media_dir.name
            for chunk in media_dir.glob("*.bin"):
                try:
                    index = int(chunk.stem)
                    size = chunk.stat().st_size
                except (ValueError, OSError):
                    continue
                self._index[(key, index)] = size
                self._total += size
                if self.is_head(index):
                    self._head_total += size
        # A smaller budget than the cache on disk shrinks it right away.
        self._evict()

    def _relayout(self) -> None:
        """Keep the cache valid when the configured chunk size changes.

        A chunk file is named by its index, so its byte offset depends on the chunk
        size it was written with. That size is recorded in ``layout.json``; a cache
        without the record (written before it existed) is judged by its largest
        file. Chunks of a whole multiple of the new size are split in place, which
        keeps warm heads without reading anything from the archive again; any other
        layout is dropped. The old tree is first renamed aside in one step and
        converted one clip at a time, so an interrupted conversion resumes on the
        next start instead of mixing two layouts.
        """
        aside = self._root.with_name(self._root.name + ".relayout")
        if not aside.exists():
            previous = self._recorded_chunk_bytes(self._root)
            if previous is None or previous == self.chunk_bytes:
                self._write_layout(self._root)
                return
            self._root.rename(aside)
        previous = self._recorded_chunk_bytes(aside) or 0
        self._root.mkdir(parents=True, exist_ok=True)
        self._write_layout(self._root)
        if previous > self.chunk_bytes and previous % self.chunk_bytes == 0:
            ratio = previous // self.chunk_bytes
            for media_dir in sorted(aside.iterdir()):
                if media_dir.is_dir():
                    self._split_clip(media_dir, self._root / media_dir.name, ratio)
        shutil.rmtree(aside, ignore_errors=True)

    def _recorded_chunk_bytes(self, root: Path) -> int | None:
        """The chunk size a cache tree was written with; None for an empty tree."""
        try:
            return int(json.loads((root / _LAYOUT).read_text())["chunk_bytes"])
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            return -1  # unreadable record: the layout is unknown and gets dropped
        if not root.is_dir():
            return None
        largest = max((chunk.stat().st_size for chunk in root.glob("*/*.bin")), default=0)
        if largest == 0:
            return None
        # Full chunks fill the size they were written with.
        return largest if largest > self.chunk_bytes else self.chunk_bytes

    def _write_layout(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        temporary = root / f".{_LAYOUT}.tmp"
        temporary.write_text(json.dumps({"chunk_bytes": self.chunk_bytes}))
        temporary.replace(root / _LAYOUT)

    def _split_clip(self, source: Path, target: Path, ratio: int) -> None:
        target.mkdir(parents=True, exist_ok=True)
        for chunk in source.glob("*.bin"):
            try:
                index = int(chunk.stem)
                data = chunk.read_bytes()
            except (ValueError, OSError):
                continue
            for part, offset in enumerate(range(0, len(data), self.chunk_bytes)):
                piece = target / f"{index * ratio + part}.bin"
                temporary = piece.with_name(f".{piece.name}.tmp")
                temporary.write_bytes(data[offset : offset + self.chunk_bytes])
                temporary.replace(piece)
        shutil.rmtree(source, ignore_errors=True)

    def _path(self, key: str, index: int) -> Path:
        return self._root / key / f"{index}.bin"

    def has(self, key: str, index: int) -> bool:
        return (key, index) in self._index

    def touch(self, key: str, index: int) -> None:
        if (key, index) in self._index:
            self._index.move_to_end((key, index))

    def read_slice(self, key: str, index: int, offset: int, length: int) -> bytes | None:
        if (key, index) not in self._index:
            return None
        path = self._path(key, index)
        try:
            with open(path, "rb") as handle:
                handle.seek(max(0, offset))
                data = handle.read(max(0, length))
        except OSError:
            self._forget(key, index)
            return None
        self._index.move_to_end((key, index))
        return data

    def write_chunk(self, key: str, index: int, data: bytes) -> None:
        directory = self._root / key
        directory.mkdir(parents=True, exist_ok=True)
        target = self._path(key, index)
        temporary = target.with_name(f".{index}.bin.tmp")
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
        previous = self._index.get((key, index))
        if previous is not None:
            self._account(index, -previous)
        self._index[(key, index)] = len(data)
        self._account(index, len(data))
        self._index.move_to_end((key, index))
        self._evict()

    def _account(self, index: int, delta: int) -> None:
        self._total += delta
        if self.is_head(index):
            self._head_total += delta

    def _forget(self, key: str, index: int) -> None:
        size = self._index.pop((key, index), None)
        if size is not None:
            self._account(index, -size)
        try:
            self._path(key, index).unlink()
        except OSError:
            pass

    def delete_key(self, key: str) -> None:
        for cache_key, size in list(self._index.items()):
            if cache_key[0] != key:
                continue
            self._index.pop(cache_key, None)
            self._account(cache_key[1], -size)
        shutil.rmtree(self._root / key, ignore_errors=True)

    def _victim(self) -> tuple[str, int]:
        """The least recently used chunk of the tier that has to give way."""
        want_head = self.head_chunks > 0 and self._head_total > self.head_budget
        for cache_key in self._index:
            if self.is_head(cache_key[1]) == want_head:
                return cache_key
        return next(iter(self._index))

    def _evict(self) -> None:
        # Heads over their share go first; otherwise playback data does, so warming
        # and watching never evict each other. Only one tier left: plain LRU.
        while self._index and (
            self._total > self.max_bytes
            or (self.head_chunks and self._head_total > self.head_budget)
        ):
            key, index = self._victim()
            size = self._index.pop((key, index))
            self._account(index, -size)
            try:
                self._path(key, index).unlink()
            except OSError:
                pass

    def stats(self) -> dict[str, object]:
        return {
            "bytes": self._total,
            "chunks": len(self._index),
            "max_bytes": self.max_bytes,
            "head_bytes": self._head_total,
            "head_budget": self.head_budget,
            "chunk_bytes": self.chunk_bytes,
            "updated_at": time.time(),
        }
