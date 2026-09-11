from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re

from tgvio.domain.archive import (
    ArchiveObject,
    ArchiveObjectRole,
    ArchivePackage,
    ArchivePackageState,
    ArchivePlan,
    archive_json_sha256,
)
from tgvio.domain.job import Job, MediaItem, MediaKind


ARCHIVE_LAYOUT_VERSION = "tgvio.archive/v1"
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f/\\]+")


class ArchivePlanningError(RuntimeError):
    pass


class ArchivePlanner:
    """Build a deterministic, network-free WebDAV archive package."""

    def __init__(self, *, remote_root: str = "") -> None:
        self._remote_root = self._normalize_remote_root(remote_root)

    def plan(self, job: Job) -> ArchivePlan:
        candidates = [
            item
            for item in sorted(job.items, key=lambda current: current.index)
            if item.kind != MediaKind.TEXT
        ]
        if not candidates:
            raise ArchivePlanningError("job has no archivable media")

        package_id = f"arc_{job.id}"
        stamp = self._job_datetime(job.created_at)
        package_name = (
            f"{stamp.strftime('%Y%m%d-%H%M%S')}__media-{len(candidates):03d}__{job.id[:10]}"
        )
        relative_remote_path = (
            f"archive/{stamp.strftime('%Y')}/{stamp.strftime('%m')}/{stamp.strftime('%d')}/"
            f"{package_name}"
        )
        remote_path = self._join_root(relative_remote_path)
        staging_path = self._join_root(f".staging/{package_id}")

        objects: list[ArchiveObject] = []
        media_manifest: list[dict[str, object]] = []
        for object_index, item in enumerate(candidates):
            local = self._canonical_path(item)
            if not local.is_file():
                raise ArchivePlanningError(f"canonical media missing for item {item.index}")
            if not item.sha256:
                raise ArchivePlanningError(f"sha256 missing for item {item.index}")
            size = local.stat().st_size
            remote_name = self._remote_name(object_index, item, local)
            remote_relpath = f"media/{remote_name}"
            objects.append(
                ArchiveObject(
                    package_id=package_id,
                    object_index=object_index,
                    item_index=item.index,
                    role=ArchiveObjectRole.MEDIA,
                    local_path=str(local),
                    remote_relpath=remote_relpath,
                    size_bytes=size,
                    sha256=item.sha256,
                )
            )
            media_manifest.append(self._manifest_item(item, remote_relpath, size))

        manifest = {
            "schema": ARCHIVE_LAYOUT_VERSION,
            "package_id": package_id,
            "job_id": job.id,
            "created_at": stamp.isoformat().replace("+00:00", "Z"),
            "media_count": len(objects),
            "media": media_manifest,
        }
        manifest_sha256 = archive_json_sha256(manifest)
        package = ArchivePackage(
            id=package_id,
            job_id=job.id,
            layout_version=ARCHIVE_LAYOUT_VERSION,
            remote_path=remote_path,
            staging_path=staging_path,
            state=ArchivePackageState.PLANNED,
            manifest=manifest,
            objects=tuple(objects),
            manifest_sha256=manifest_sha256,
        )
        summary = {
            "media_total": len(objects),
            "total_bytes": sum(current.size_bytes for current in objects),
            "remote_path": remote_path,
            "staging_path": staging_path,
            "manifest_sha256": manifest_sha256,
        }
        return ArchivePlan(package=package, summary=summary)

    @staticmethod
    def render_tree(plan: ArchivePlan) -> str:
        package = plan.package
        lines = [f"{package.remote_path}/", "├── media/"]
        for index, item in enumerate(package.objects):
            branch = "└──" if index == len(package.objects) - 1 else "├──"
            lines.append(f"│   {branch} {Path(item.remote_relpath).name}")
        lines.extend(["├── manifest.json", "└── _COMPLETE.json"])
        return "\n".join(lines)

    @staticmethod
    def _canonical_path(item: MediaItem) -> Path:
        override = item.metadata.get("canonical_path")
        value = str(override).strip() if override else str(item.local_path or "").strip()
        if not value:
            raise ArchivePlanningError(f"canonical path missing for item {item.index}")
        return Path(value)

    @classmethod
    def _remote_name(cls, object_index: int, item: MediaItem, local: Path) -> str:
        original = (item.name or local.name or f"media-{object_index + 1}").strip()
        name = Path(original).name
        name = _CONTROL_RE.sub("_", name).strip().strip(".")
        if not name or name in {".", ".."}:
            name = f"media-{object_index + 1:03d}.bin"
        return f"{object_index + 1:03d}__{name[:180]}"

    @staticmethod
    def _manifest_item(
        item: MediaItem,
        remote_relpath: str,
        size_bytes: int,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "index": item.index + 1,
            "kind": item.kind.value,
            "path": remote_relpath,
            "original_name": item.name or Path(item.local_path or remote_relpath).name,
            "size_bytes": size_bytes,
            "sha256": item.sha256 or "",
        }
        optional = {
            "mime_type": item.mime_type,
            "width": item.width,
            "height": item.height,
            "duration_seconds": item.duration_seconds,
            "container": item.container,
            "codec": item.codec,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        return payload

    @staticmethod
    def _job_datetime(value: str | None) -> datetime:
        if value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except ValueError:
                pass
        return datetime.now(timezone.utc)

    def _join_root(self, relative: str) -> str:
        return f"{self._remote_root}/{relative}" if self._remote_root else relative

    @staticmethod
    def _normalize_remote_root(value: str) -> str:
        raw = str(value or "").strip().strip("/")
        if not raw:
            return ""
        parts = [part for part in raw.split("/") if part]
        if any(
            part in {".", ".."}
            or "\\" in part
            or re.search(r"[\x00-\x1f\x7f]", part)
            for part in parts
        ):
            raise ValueError("unsafe archive remote root")
        return "/".join(parts)
