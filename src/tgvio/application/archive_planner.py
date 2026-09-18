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
    ArchiveProfileSnapshot,
    archive_json_sha256,
)
from tgvio.domain.job import Job, MediaItem, MediaKind


ARCHIVE_LAYOUT_VERSION = "tgvio.archive/v1"
ARCHIVE_LAYOUT_VERSION_V2 = "tgvio.archive/v2"
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f/\\]+")


class ArchivePlanningError(RuntimeError):
    pass


class ArchivePlanner:
    """Build a deterministic, network-free WebDAV archive package."""

    def __init__(
        self,
        *,
        remote_root: str = "",
        profile: ArchiveProfileSnapshot | None = None,
        layout: str = "v1",
    ) -> None:
        self._remote_root = self._normalize_remote_root(remote_root)
        self._profile = profile or ArchiveProfileSnapshot()
        self._layout = "v2" if str(layout).lower() == "v2" else "v1"
        self._layout_version = (
            ARCHIVE_LAYOUT_VERSION_V2 if self._layout == "v2" else ARCHIVE_LAYOUT_VERSION
        )

    @property
    def profile(self) -> ArchiveProfileSnapshot:
        return self._profile

    @property
    def layout(self) -> str:
        return self._layout

    def plan(
        self,
        job: Job,
        *,
        day: str | None = None,
        day_seq: int | None = None,
    ) -> ArchivePlan:
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
        if self._layout == "v2" and day and day_seq is not None:
            relative_remote_path = f"{day}/{int(day_seq)}"
        else:
            relative_remote_path = (
                f"archive/{stamp.strftime('%Y')}/{stamp.strftime('%m')}/{stamp.strftime('%d')}/"
                f"{package_name}"
            )
        remote_path = self._join_root(relative_remote_path)
        staging_path = self._join_root(f".staging/{package_id}")

        objects: list[ArchiveObject] = []
        media_manifest: list[dict[str, object]] = []
        used_relpaths: set[str] = set()
        for object_index, item in enumerate(candidates):
            local = self._canonical_path(item)
            if not local.is_file():
                raise ArchivePlanningError(f"canonical media missing for item {item.index}")
            if not item.sha256:
                raise ArchivePlanningError(f"sha256 missing for item {item.index}")
            size = local.stat().st_size
            remote_name = self._remote_name(object_index, item, local, layout=self._layout)
            remote_relpath = remote_name if self._layout == "v2" else f"media/{remote_name}"
            # Two items can legitimately share content (duplicate forwards), which
            # would otherwise collide on UNIQUE(package_id, remote_relpath) and
            # fail the whole job during prepare. Keep every object addressable.
            remote_relpath = self._unique_relpath(remote_relpath, used_relpaths)
            used_relpaths.add(remote_relpath)
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
            "schema": self._layout_version,
            "package_id": package_id,
            "job_id": job.id,
            "created_at": stamp.isoformat().replace("+00:00", "Z"),
            "archive_profile": {
                "id": self._profile.profile_id,
                "policy": self._profile.policy.value,
                "policy_version": self._profile.policy_version,
            },
            "media_count": len(objects),
            "media": media_manifest,
        }
        manifest_sha256 = archive_json_sha256(manifest)
        package = ArchivePackage(
            id=package_id,
            job_id=job.id,
            layout_version=self._layout_version,
            remote_path=remote_path,
            staging_path=staging_path,
            state=ArchivePackageState.PLANNED,
            manifest=manifest,
            objects=tuple(objects),
            manifest_sha256=manifest_sha256,
            archive_profile_id=self._profile.profile_id,
            archive_policy=self._profile.policy,
            archive_policy_version=self._profile.policy_version,
        )
        summary = {
            "media_total": len(objects),
            "total_bytes": sum(current.size_bytes for current in objects),
            "remote_path": remote_path,
            "staging_path": staging_path,
            "manifest_sha256": manifest_sha256,
            "archive_profile_id": self._profile.profile_id,
            "archive_policy": self._profile.policy.value,
            "archive_policy_version": self._profile.policy_version,
        }
        return ArchivePlan(package=package, summary=summary)

    @staticmethod
    def render_tree(plan: ArchivePlan) -> str:
        package = plan.package
        if package.layout_version == ARCHIVE_LAYOUT_VERSION_V2:
            lines = [f"{package.remote_path}/"]
            for index, item in enumerate(package.objects):
                branch = "└──" if index == len(package.objects) - 1 else "├──"
                lines.append(f"{branch} {Path(item.remote_relpath).name}")
            lines.extend(["├── manifest.json", "└── _COMPLETE.json"])
            return "\n".join(lines)
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

    @staticmethod
    def _unique_relpath(relpath: str, used: set[str]) -> str:
        """Return a package-unique relative path, adding a stable ``-N`` suffix."""

        if relpath not in used:
            return relpath
        path = Path(relpath)
        suffix = path.suffix
        stem = path.name[: -len(suffix)] if suffix else path.name
        parent = str(path.parent)
        counter = 2
        while True:
            candidate_name = f"{stem}-{counter}{suffix}"
            candidate = (
                f"{parent}/{candidate_name}"
                if parent not in {"", "."}
                else candidate_name
            )
            if candidate not in used:
                return candidate
            counter += 1

    @classmethod
    def _remote_name(
        cls,
        object_index: int,
        item: MediaItem,
        local: Path,
        *,
        layout: str = "v1",
    ) -> str:
        original = (item.name or local.name or f"media-{object_index + 1}").strip()
        name = Path(original).name
        name = _CONTROL_RE.sub("_", name).strip().strip(".")
        if layout == "v2":
            digest = (item.sha256 or "")[:12]
            suffix = Path(name).suffix[:16]
            if digest:
                return f"{digest}{suffix}"
            return f"media-{object_index + 1:03d}{suffix}"
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
