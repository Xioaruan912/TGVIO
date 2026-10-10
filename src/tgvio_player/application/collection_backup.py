"""Copies of manual-collection members in one storage folder per collection.

A collection named "视频" keeps its members under ``<player root>/视频/``, like the
favorites folder keeps favorites. The worker reconciles what the storage holds
(``collection_backups``) with the current memberships: a new member is copied
there with a server-side copy (the archive original never moves), a renamed
collection's copies are moved to the new folder, and a copy whose membership or
collection is gone is removed. Every step is idempotent and retried with a
back-off, so a failure or restart only does what is left. Smart collections are
rule views, not owned sets, and are not copied.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import PurePosixPath
import re
import time
from typing import Awaitable, Callable

from tgvio_player.application.ports import WebDavWriteClient, WebDavWriteError
from tgvio_player.domain.storage_settings import safe_storage_relpath

_LOG = logging.getLogger("tgvio_player.collection_backup")
_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")
RETRY_BASE_SECONDS = 30
RETRY_MAX_SECONDS = 3600


def folder_names(collections: list[tuple[str, str, str]], reserved: set[str]) -> dict[str, str]:
    """One storage folder per collection id, named after the collection.

    Slashes become their full-width forms so a name stays one folder; a name that
    is empty, a dot name, a reserved folder (favorites) or already taken by an
    older collection gets a numbered suffix. Oldest collections keep the plain name.
    """
    taken = {name.casefold() for name in reserved}
    folders: dict[str, str] = {}
    for collection_id, name, _created in collections:
        base = _UNSAFE.sub("", name).replace("/", "／").replace("\\", "＼").strip().strip(".")
        base = base or "集合"
        candidate, number = base, 2
        while candidate.casefold() in taken:
            candidate = f"{base} ({number})"
            number += 1
        taken.add(candidate.casefold())
        folders[collection_id] = candidate
    return folders


def _top(relpath: str) -> str:
    """The collection folder a copy lives in (its first path segment)."""
    return PurePosixPath(relpath).parts[0]


def retry_delay(attempts: int) -> int:
    return min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2 ** max(0, attempts - 1))


@dataclass(frozen=True, slots=True)
class CollectionBackupResult:
    copied: int = 0
    moved: int = 0
    removed: int = 0
    failed: int = 0

    @property
    def processed(self) -> int:
        return self.copied + self.moved + self.removed + self.failed


class CollectionBackupService:
    def __init__(
        self,
        repository,
        writer: Callable[[], WebDavWriteClient],
        source_location: Callable[[str], Awaitable[tuple[str, str] | None]],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repository = repository
        self._writer = writer
        self._source_location = source_location
        self._clock = clock

    async def sync_once(self, limit: int = 4) -> CollectionBackupResult:
        """Do up to ``limit`` due copy, move or remove steps."""
        now = int(self._clock())
        settings = await self._repository.get_storage_settings()
        root = settings.player_root
        folders = folder_names(
            await self._repository.collection_backup_collections(), {settings.favorites_dir}
        )
        wanted = {
            (m.collection_id, m.media_id): folders[m.collection_id]
            for m in await self._repository.collection_backup_members()
            if m.collection_id in folders
        }
        rows = {(r.collection_id, r.media_id): r for r in await self._repository.collection_backup_rows()}
        # Paths in use per collection folder, kept current as this pass places copies.
        placed = {key: row.relpath for key, row in rows.items() if row.relpath is not None}
        # Folders this pass moved or removed copies out of; emptied ones are removed.
        vacated: set[str] = set()
        copied = moved = removed = failed = 0
        steps = 0
        # Copies first: a viewer waits to see a new member in its folder.
        for key, folder in wanted.items():
            if steps >= limit:
                break
            row = rows.get(key)
            if row is not None and row.next_attempt_at > now:
                continue
            if row is not None and row.relpath is not None and PurePosixPath(row.relpath).parent.as_posix() == folder:
                continue
            steps += 1
            if row is not None and row.relpath is not None:
                vacated.add(_top(row.relpath))
            try:
                outcome = await self._place(root, folder, key, row, placed)
            except Exception as exc:  # noqa: BLE001 - every failure is retried later
                failed += 1
                await self._fail(key, row, exc, now)
                continue
            if outcome == "moved":
                moved += 1
            else:
                copied += 1
        for key, row in rows.items():
            if steps >= limit:
                break
            if key in wanted or row.next_attempt_at > now:
                continue
            steps += 1
            try:
                if row.relpath is not None:
                    receipt = await self._writer().delete(self._remote(root, row.relpath))
                    if not receipt.deleted:
                        raise WebDavWriteError("delete", "delete_not_confirmed", receipt.status_code)
                await self._repository.drop_collection_backup(*key)
                if row.relpath is not None:
                    vacated.add(_top(row.relpath))
                placed.pop(key, None)
                removed += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                await self._fail(key, row, exc, now)
        await self._remove_empty_folders(root, vacated, set(wanted.values()), placed, settings.favorites_dir)
        return CollectionBackupResult(copied, moved, removed, failed)

    async def _remove_empty_folders(
        self, root: str, vacated: set[str], wanted: set[str], placed: dict, favorites_dir: str
    ) -> None:
        """A collection folder that holds no copy any more goes too: emptied,
        deleted or renamed collections leave no empty folders behind. The folder
        only ever holds this worker's copies; the favorites folder is never touched."""
        in_use = wanted | {_top(path) for path in placed.values()}
        for folder in vacated - in_use - {favorites_dir}:
            try:
                await self._writer().delete(self._remote(root, folder))
            except Exception as exc:  # noqa: BLE001 - an empty folder is harmless
                _LOG.warning("player.collection_backup.folder_kept error=%s", getattr(exc, "category", type(exc).__name__))

    async def _place(self, root, folder, key, row, placed: dict) -> str:
        collection_id, media_id = key
        details = await self._repository.active_media_details(media_id)
        location = await self._source_location(media_id)
        if details is None or location is None:
            raise WebDavWriteError("copy", "source_not_found")
        size_bytes = int(details["size_bytes"])
        source = safe_storage_relpath(f"{location[0]}/{location[1]}")
        name = PurePosixPath(source).name
        relpath = f"{folder}/{name}"
        # Two different videos with one file name in one folder: the later one
        # goes in a sub-folder named after it.
        if any(
            other_path == relpath and other_key != key and other_key[0] == collection_id
            for other_key, other_path in placed.items()
        ):
            relpath = f"{folder}/{media_id}/{name}"
        relpath = safe_storage_relpath(relpath)
        writer = self._writer()
        target = self._remote(root, relpath)
        await writer.ensure_directory(str(PurePosixPath(target).parent))
        outcome = "copied"
        if row is not None and row.relpath is not None:
            try:
                await writer.move(self._remote(root, row.relpath), target, overwrite=True)
                outcome = "moved"
            except WebDavWriteError:
                outcome = "copied"  # the old copy is gone or unmovable; copy afresh
        if outcome == "copied":
            remote = await writer.stat(target)
            if remote is None or remote.size_bytes != size_bytes:
                copier = getattr(writer, "copy", None)
                if copier is None:
                    raise WebDavWriteError("copy", "copy_unsupported")
                await copier(source, target)
        remote = await writer.stat(target)
        if remote is None or remote.size_bytes != size_bytes:
            raise WebDavWriteError("copy", "verification_mismatch")
        await self._repository.save_collection_backup(collection_id, media_id, relpath, size_bytes)
        placed[key] = relpath
        return outcome

    async def _fail(self, key, row, exc: Exception, now: int) -> None:
        attempts = (row.attempts if row is not None else 0) + 1
        category = getattr(exc, "category", type(exc).__name__)
        wait = retry_delay(attempts)
        await self._repository.fail_collection_backup(
            *key, error=str(category), next_attempt_at=now + wait
        )
        _LOG.warning(
            "player.collection_backup.retry collection=%s media=%s attempt=%s error=%s wait_s=%s",
            key[0][:8], key[1][:12], attempts, category, wait,
        )

    @staticmethod
    def _remote(root: str, relpath: str) -> str:
        return safe_storage_relpath(f"{safe_storage_relpath(root)}/{safe_storage_relpath(relpath)}")
