from __future__ import annotations

from datetime import date
from hashlib import sha256
from pathlib import PurePosixPath
import re


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_FOLDER = re.compile(r"^folder_[0-9a-f]{64}$")
_DAY = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def valid_library_date(value: str) -> bool:
    if not _DAY.fullmatch(value):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def directory_date(path: str) -> tuple[str | None, str]:
    """Directory semantics only: never infer dates from manifest timestamps."""
    parts = PurePosixPath(path).parent.parts
    if parts and valid_library_date(parts[-1]):
        return parts[-1], "directory_v2"
    if len(parts) >= 3:
        legacy = "-".join(parts[-3:])
        if valid_library_date(legacy):
            return legacy, "directory_legacy_utc"
    return None, "unknown"


def folder_identity(package_id: str) -> str:
    # One-way, stable namespace identity; not a path encoding or client capability.
    return "folder_" + sha256(("tgvio-library-folder-v1:" + package_id).encode()).hexdigest()


# All projections and counts share the same active original-video location set.
# SQLite UDFs derive directory metadata without migrations or discovery changes.
_ELIGIBLE = """
WITH eligible AS (
    SELECT DISTINCT m.media_id, m.duration_seconds, cp.package_id,
           library_folder_id(cp.package_id) AS folder_id,
           library_date(cp.remote_path) AS day,
           library_date_basis(cp.remote_path) AS basis
    FROM media m
    JOIN media_locations ml ON ml.media_id=m.media_id
    JOIN catalog_packages cp ON cp.package_id=ml.package_id
    WHERE m.active=1 AND m.kind='video' AND ml.active=1 AND cp.active=1
      AND NOT EXISTS (SELECT 1 FROM media_variants mv WHERE mv.variant_media_id=m.media_id)
)
"""


class PlayerLibraryRepositoryMixin:
    def _library_connection(self):
        conn = self._require()
        conn.create_function("library_folder_id", 1, folder_identity, deterministic=True)
        conn.create_function("library_date", 1, lambda p: directory_date(p)[0], deterministic=True)
        conn.create_function("library_date_basis", 1, lambda p: directory_date(p)[1], deterministic=True)
        return conn

    @staticmethod
    def _library_folder(row) -> dict[str, object]:
        identity = str(row["folder_id"])
        # Deliberately not a remote basename: legacy names can contain private data.
        return {"id": identity, "label": "批次 " + identity[7:19],
                "date": row["day"], "date_basis": row["basis"],
                "video_count": int(row["video_count"])}

    async def library_dates(self) -> dict[str, object]:
        conn = self._library_connection()
        rows = conn.execute(_ELIGIBLE + """
            SELECT day, CASE WHEN COUNT(DISTINCT basis)>1 THEN 'mixed' ELSE MIN(basis) END AS basis,
                   COUNT(DISTINCT media_id) AS video_count, COUNT(DISTINCT folder_id) AS folder_count
            FROM eligible GROUP BY day ORDER BY day IS NULL, day DESC
        """).fetchall()
        total = conn.execute(_ELIGIBLE + "SELECT COUNT(DISTINCT media_id) FROM eligible").fetchone()[0]
        return {"items": [{"date": r["day"], "basis": r["basis"],
                           "video_count": r["video_count"], "folder_count": r["folder_count"]}
                          for r in rows], "total_videos": int(total)}

    async def library_folders(self, *, date_filter: str | None = None,
                              media_id: str | None = None) -> dict[str, object]:
        if (date_filter is None) == (media_id is None):
            raise ValueError("exactly one folder filter required")
        if media_id is not None:
            if not _HEX64.fullmatch(media_id):
                raise ValueError("invalid media id")
            # Membership subquery filters folders, not their full video counts.
            clause = "folder_id IN (SELECT folder_id FROM eligible WHERE media_id=?)"
            params = (media_id,)
        else:
            if date_filter != "unknown" and not valid_library_date(date_filter):
                raise ValueError("invalid directory date")
            clause = "day IS ?"
            params = (None if date_filter == "unknown" else date_filter,)
        rows = self._library_connection().execute(_ELIGIBLE + f"""
            SELECT folder_id, day, basis, COUNT(DISTINCT media_id) AS video_count
            FROM eligible WHERE {clause} GROUP BY folder_id, day, basis
            ORDER BY day IS NULL, day DESC, folder_id
        """, params).fetchall()
        items = [self._library_folder(r) for r in rows]
        return {"items": items, "total": len(items)}

    async def library_video_page(self, folder_id: str, *, category: str = "all",
                                 limit: int = 20, cursor: str | None = None,
                                 long_seconds: float = 300) -> dict[str, object] | None:
        if not _FOLDER.fullmatch(folder_id):
            raise ValueError("invalid folder")
        if category not in {"all", "short", "long"} or not 1 <= limit <= 20:
            raise ValueError("invalid paging")
        if cursor is not None and not _HEX64.fullmatch(cursor):
            raise ValueError("invalid cursor")
        conn = self._library_connection()
        folder = conn.execute(_ELIGIBLE + """
            SELECT folder_id, day, basis, COUNT(DISTINCT media_id) AS video_count
            FROM eligible WHERE folder_id=? GROUP BY folder_id, day, basis
        """, (folder_id,)).fetchone()
        if folder is None:
            return None
        clauses, params = ["folder_id=?"], [folder_id]
        if category == "long":
            clauses.append("duration_seconds > ?")
            params.append(float(long_seconds))
        elif category == "short":
            # Null duration is short in existing MediaDto category semantics.
            clauses.append("(duration_seconds IS NULL OR duration_seconds <= ?)")
            params.append(float(long_seconds))
        predicate = " AND ".join(clauses)
        total = conn.execute(_ELIGIBLE + f"SELECT COUNT(DISTINCT media_id) FROM eligible WHERE {predicate}",
                             tuple(params)).fetchone()[0]
        if cursor is not None:
            # Cursor is an ordering key, not a membership capability. Scope remains
            # in folder/category predicates even if the tail row was retired.
            clauses.append("media_id > ?")
            params.append(cursor)
        rows = conn.execute(_ELIGIBLE + "SELECT DISTINCT media_id FROM eligible WHERE " +
                            " AND ".join(clauses) + " ORDER BY media_id LIMIT ?",
                            (*params, limit + 1)).fetchall()
        has_more = len(rows) > limit
        ids = [str(r["media_id"]) for r in rows[:limit]]
        return {"media_ids": ids, "has_more": has_more,
                "next_cursor": ids[-1] if has_more else None,
                "total": int(total), "folder": self._library_folder(folder)}
