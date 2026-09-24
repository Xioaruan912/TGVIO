from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import os
import shutil
import time


class RangeStore:
    """Byte-bounded, chunk-granular, LRU disk cache for media byte ranges.

    Files live at ``<root>/<key>/<index>.bin`` and are only created once a whole
    chunk has been fetched, so a cached file is always complete. Eviction is by
    least-recently-used across all media, bounded by ``max_bytes``.
    """

    def __init__(self, root: Path, *, chunk_bytes: int, max_bytes: int) -> None:
        self._root = Path(root)
        self.chunk_bytes = max(64 * 1024, int(chunk_bytes))
        self.max_bytes = max(self.chunk_bytes, int(max_bytes))
        self._index: "OrderedDict[tuple[str, int], int]" = OrderedDict()
        self._total = 0

    @property
    def total_bytes(self) -> int:
        return self._total

    @property
    def chunk_count(self) -> int:
        return len(self._index)

    def open(self) -> None:
        self._index.clear()
        self._total = 0
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
        self._evict()

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
            self._total -= previous
        self._index[(key, index)] = len(data)
        self._total += len(data)
        self._index.move_to_end((key, index))
        self._evict()

    def _forget(self, key: str, index: int) -> None:
        size = self._index.pop((key, index), None)
        if size is not None:
            self._total -= size
        try:
            self._path(key, index).unlink()
        except OSError:
            pass

    def delete_key(self, key: str) -> None:
        for cache_key, size in list(self._index.items()):
            if cache_key[0] != key:
                continue
            self._index.pop(cache_key, None)
            self._total -= size
        shutil.rmtree(self._root / key, ignore_errors=True)

    def _evict(self) -> None:
        while self._total > self.max_bytes and self._index:
            (key, index), size = self._index.popitem(last=False)
            self._total -= size
            try:
                self._path(key, index).unlink()
            except OSError:
                pass

    def stats(self) -> dict[str, object]:
        return {
            "bytes": self._total,
            "chunks": len(self._index),
            "max_bytes": self.max_bytes,
            "chunk_bytes": self.chunk_bytes,
            "updated_at": time.time(),
        }
