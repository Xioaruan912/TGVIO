"""The local cover mirror: a content-addressed store with a bounded budget.

The mirror is a pure cache. Nothing here decides whether a cover may be served — the cover
route owns that. What this module owns is the invariant that matters: a file under the
mirror's own name is complete, and a name that is not a content hash is not a key.

The archive already names every cover by the sha256 of its own bytes
(`cover/backfill/<sha256>.jpg`), so the key needs no invalidation logic at all: the same
bytes are always the same file.
"""
from __future__ import annotations

from collections.abc import Collection, Iterator
from dataclasses import dataclass
import os
from pathlib import Path
import re
import tempfile

# `<64 lowercase hex>.jpg`, and nothing else. A path this cannot read is not mirrored.
CONTENT_ADDRESSED = re.compile(r"^([0-9a-f]{64})\.jpg$")


@dataclass
class CoverMirrorCounters:
    """What the route and the warm loop did, for `/healthz`. Process-lifetime, like the
    other cover counters: a restart starts them over."""

    hits: int = 0
    misses: int = 0
    write_failed: int = 0
    warm_pending: int = 0
    warm_failed: int = 0


class CoverMirror:
    def __init__(self, root: Path, budget_bytes: int) -> None:
        self.root = Path(root)
        self.budget_bytes = max(0, int(budget_bytes))

    def key_for(self, remote_relpath: str) -> str | None:
        """The content key of a catalog-declared cover path, or None if it is not one.

        Only the basename is read: a path this module cannot recognise is never mirrored,
        and nothing here is ever used as a filesystem path of its own.
        """
        match = CONTENT_ADDRESSED.fullmatch(Path(str(remote_relpath)).name)
        return match.group(1) if match else None

    def path_for(self, key: str) -> Path:
        return self.root / f"{key}.jpg"

    def exists(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def has(self, key: str, size: int) -> bool:
        """A hit needs the size the catalog declares, not just a file with that name."""
        try:
            return self.path_for(key).stat().st_size == int(size) and self.path_for(key).is_file()
        except (OSError, TypeError, ValueError):
            return False

    def read(self, key: str) -> bytes | None:
        try:
            return self.path_for(key).read_bytes()
        except OSError:
            return None

    def write(self, key: str, payload: bytes) -> None:
        """Write through a temporary file: a half-written cover never carries a real name."""
        self.root.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.root, prefix=".tmp-")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path_for(key))
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def stats(self) -> tuple[int, int]:
        files = list(self._files())
        return len(files), sum(path.stat().st_size for path in files)

    def sweep(self, keep: Collection[str]) -> int:
        """Drop what no active cover references, then the oldest until the budget holds."""
        kept = {str(key) for key in keep}
        removed = 0
        for path in self._files():
            if path.stem in kept:
                continue
            path.unlink(missing_ok=True)
            removed += 1
        remaining = sorted(self._files(), key=lambda path: path.stat().st_mtime)
        total = sum(path.stat().st_size for path in remaining)
        for path in remaining:
            if total <= self.budget_bytes:
                break
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            removed += 1
            total -= size
        return removed

    def _files(self) -> Iterator[Path]:
        if not self.root.is_dir():
            return iter(())
        return (path for path in self.root.iterdir() if CONTENT_ADDRESSED.fullmatch(path.name))
