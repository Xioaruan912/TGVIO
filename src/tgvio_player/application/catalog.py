from __future__ import annotations

import hashlib
import json
from typing import Any

from tgvio_player.application.ports import ArchiveCatalogSource, PlayerCatalogRepository
from tgvio_player.domain.catalog import (
    ArchivePackageCandidate,
    CatalogCover,
    CatalogLocation,
    CatalogMedia,
    CatalogPackage,
    CatalogSyncResult,
    CatalogValidationError,
    COVER_ALGORITHMS,
    COVER_MIME_TYPES,
    MAX_COVER_BYTES,
    require_sha256,
    safe_remote_path,
)


_COMPLETE_SCHEMA = "tgvio.archive.complete/v1"
_MANIFEST_SCHEMAS = {"tgvio.archive/v1", "tgvio.archive/v2", "tgvio.archive/v3"}


def _canonical_json_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CatalogSyncService:
    """Project committed Archive packages into the Player-owned catalog."""

    def __init__(
        self,
        source: ArchiveCatalogSource,
        repository: PlayerCatalogRepository,
    ) -> None:
        self._source = source
        self._repository = repository

    async def sync_once(self) -> CatalogSyncResult:
        discovery = await self._source.discover()
        committed = 0
        rejected = 0
        errors: list[str] = []
        seen_package_ids: set[str] = set()
        discovered_package_ids: set[str] = set()

        for candidate in discovery.packages:
            discovered_id = self._candidate_package_id(candidate)
            if discovered_id is not None:
                discovered_package_ids.add(discovered_id)
            try:
                package = self._validate(candidate)
                await self._repository.apply_package(package)
            except (CatalogValidationError, TypeError, ValueError) as exc:
                rejected += 1
                errors.append(self._safe_error(candidate.remote_path, exc))
                continue
            committed += 1
            seen_package_ids.add(package.package_id)

        inactive = 0
        if discovery.complete_scan:
            # A package that is still present but temporarily malformed must not
            # be mistaken for a confirmed remote deletion. Preserve its last
            # known-good catalog projection and retry validation next sync.
            inactive = await self._repository.deactivate_packages_not_seen(
                seen_package_ids | discovered_package_ids
            )
        await self._repository.refresh_media_activity()
        active_videos = await self._repository.count_active_videos()
        return CatalogSyncResult(
            discovered=len(discovery.packages),
            committed=committed,
            rejected=rejected,
            inactive_packages=inactive,
            active_videos=active_videos,
            errors=tuple(errors),
        )

    @classmethod
    def _validate(cls, candidate: ArchivePackageCandidate) -> CatalogPackage:
        remote_path = safe_remote_path(candidate.remote_path, relative=False)
        manifest = candidate.manifest
        complete = candidate.complete
        if not isinstance(manifest, dict):
            raise CatalogValidationError("archive manifest must be an object")
        if not isinstance(complete, dict):
            raise CatalogValidationError("archive complete marker must be an object")

        if str(manifest.get("schema") or "") not in _MANIFEST_SCHEMAS:
            raise CatalogValidationError("unsupported archive manifest schema")
        if str(complete.get("schema") or "") != _COMPLETE_SCHEMA:
            raise CatalogValidationError("unsupported archive complete schema")

        package_id = str(manifest.get("package_id") or "").strip()
        if not package_id or package_id != str(complete.get("package_id") or "").strip():
            raise CatalogValidationError("archive package id mismatch")

        manifest_hash = _canonical_json_sha256(manifest)
        marker_hash = require_sha256(
            complete.get("manifest_sha256"),
            label="complete manifest_sha256",
        )
        if manifest_hash != marker_hash:
            raise CatalogValidationError("archive manifest hash mismatch")

        media_items = manifest.get("media")
        if not isinstance(media_items, list):
            raise CatalogValidationError("archive manifest media must be a list")
        # Optional, and never a reason to reject the package itself.
        manifest_algorithm = str(manifest.get("cover_algorithm") or "").strip()
        if int(manifest.get("media_count", -1)) != len(media_items):
            raise CatalogValidationError("archive manifest media_count mismatch")
        if int(complete.get("media_count", -1)) != len(media_items):
            raise CatalogValidationError("archive complete media_count mismatch")

        media: list[CatalogMedia] = []
        locations: list[CatalogLocation] = []
        covers: list[CatalogCover] = []
        total_bytes = 0
        for raw in media_items:
            if not isinstance(raw, dict):
                raise CatalogValidationError("archive media entry must be an object")
            digest = require_sha256(raw.get("sha256"), label="media sha256")
            relpath = safe_remote_path(raw.get("path"), relative=True)
            kind = str(raw.get("kind") or "").strip().lower()
            if not kind:
                raise CatalogValidationError("archive media kind is missing")
            size_bytes = int(raw.get("size_bytes", -1))
            if size_bytes < 0:
                raise CatalogValidationError("archive media size is invalid")
            total_bytes += size_bytes
            media.append(
                CatalogMedia(
                    media_id=digest,
                    kind=kind,
                    size_bytes=size_bytes,
                    mime_type=cls._optional_text(raw, "mime_type"),
                    width=cls._optional_int(raw, "width"),
                    height=cls._optional_int(raw, "height"),
                    duration_seconds=cls._optional_float(raw, "duration_seconds"),
                    container=cls._optional_text(raw, "container"),
                    codec=cls._optional_text(raw, "codec"),
                    variant_of=cls._optional_text(raw, "variant_of"),
                    label=cls._optional_text(raw, "resolution_label"),
                    bitrate_bps=cls._optional_int(raw, "bitrate_bps"),
                )
            )
            locations.append(
                CatalogLocation(
                    media_id=digest,
                    package_id=package_id,
                    remote_relpath=relpath,
                )
            )
            cover = cls._cover_entry(
                raw,
                media_id=digest,
                kind=kind,
                package_id=package_id,
                manifest_algorithm=manifest_algorithm,
            )
            if cover is not None:
                covers.append(cover)

        if int(complete.get("total_bytes", -1)) != total_bytes:
            raise CatalogValidationError("archive complete total_bytes mismatch")

        return CatalogPackage(
            package_id=package_id,
            remote_path=remote_path,
            manifest_sha256=manifest_hash,
            manifest_etag=candidate.manifest_etag,
            complete_etag=candidate.complete_etag,
            media=tuple(media),
            locations=tuple(locations),
            covers=tuple(covers),
        )

    @classmethod
    def _cover_entry(
        cls,
        raw: dict[str, Any],
        *,
        media_id: str,
        kind: str,
        package_id: str,
        manifest_algorithm: str,
    ) -> CatalogCover | None:
        """Read one optional cover entry, or ``None`` when it is not usable.

        A cover is decorative metadata, so anything that does not satisfy the
        contract - an unknown algorithm, a traversal path, an oversized still,
        a non-image type - is dropped instead of rejecting the package. Hiding a
        playable video because its thumbnail is odd would be a much worse
        failure than showing a placeholder.
        """
        if kind != "video":
            return None
        payload = raw.get("cover")
        if not isinstance(payload, dict):
            return None
        algorithm = str(payload.get("algorithm") or "").strip()
        if algorithm not in COVER_ALGORITHMS:
            return None
        if manifest_algorithm and manifest_algorithm != algorithm:
            return None
        mime_type = str(payload.get("mime_type") or "").split(";", 1)[0].strip().lower()
        if mime_type not in COVER_MIME_TYPES:
            return None
        size_bytes = payload.get("size_bytes")
        # JSON booleans, floats and strings are not byte counts. Strict typing
        # also avoids int(infinity) aborting a sync for decorative metadata.
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
            return None
        if size_bytes <= 0 or size_bytes > MAX_COVER_BYTES:
            return None
        try:
            relpath = safe_remote_path(payload.get("path"), relative=True)
        except CatalogValidationError:
            return None
        return CatalogCover(
            media_id=media_id,
            package_id=package_id,
            remote_relpath=relpath,
            size_bytes=size_bytes,
            mime_type=mime_type,
            algorithm=algorithm,
        )

    @staticmethod
    def _optional_text(payload: dict[str, Any], key: str) -> str | None:
        value = payload.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _optional_int(payload: dict[str, Any], key: str) -> int | None:
        value = payload.get(key)
        return None if value is None else int(value)

    @staticmethod
    def _optional_float(payload: dict[str, Any], key: str) -> float | None:
        value = payload.get(key)
        return None if value is None else float(value)

    @staticmethod
    def _candidate_package_id(
        candidate: ArchivePackageCandidate,
    ) -> str | None:
        if not isinstance(candidate.manifest, dict) or not isinstance(candidate.complete, dict):
            return None
        manifest_id = str(candidate.manifest.get("package_id") or "").strip()
        complete_id = str(candidate.complete.get("package_id") or "").strip()
        if manifest_id and manifest_id == complete_id:
            return manifest_id
        return None

    @staticmethod
    def _safe_error(remote_path: str, exc: Exception) -> str:
        leaf = str(remote_path or "").rstrip("/").rsplit("/", 1)[-1]
        return f"{leaf or 'package'}: {type(exc).__name__}"
