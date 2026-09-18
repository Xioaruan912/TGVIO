from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tgvio.application.archive_planner import (
    ARCHIVE_LAYOUT_VERSION,
    ArchivePlanner,
    ArchivePlanningError,
)
from tgvio.domain.archive import ArchivePolicy, ArchiveProfileSnapshot
from tgvio.domain.job import Job, JobState, MediaItem, MediaKind


class ArchivePlannerTests(unittest.TestCase):
    def _job(self, root: Path, count: int) -> Job:
        kinds = (MediaKind.PHOTO, MediaKind.VIDEO, MediaKind.DOCUMENT)
        items: list[MediaItem] = []
        for index in range(count):
            kind = kinds[index % len(kinds)]
            suffix = {
                MediaKind.PHOTO: ".jpg",
                MediaKind.VIDEO: ".mp4",
                MediaKind.DOCUMENT: ".pdf",
            }[kind]
            path = root / f"item-{index + 1}{suffix}"
            payload = (f"payload-{index}-" * 3).encode()
            path.write_bytes(payload)
            items.append(
                MediaItem(
                    index=index,
                    kind=kind,
                    source=f"fixture:{index}",
                    local_path=str(path),
                    name=f"Original {index + 1}{suffix}",
                    size_bytes=len(payload),
                    mime_type={
                        MediaKind.PHOTO: "image/jpeg",
                        MediaKind.VIDEO: "video/mp4",
                        MediaKind.DOCUMENT: "application/pdf",
                    }[kind],
                    duration_seconds=12.5 if kind == MediaKind.VIDEO else None,
                    container="mp4" if kind == MediaKind.VIDEO else None,
                    codec="h264" if kind == MediaKind.VIDEO else None,
                    sha256=f"sha-{index:064d}"[-64:],
                )
            )
        return Job(
            id="a84c19d2f00112233445566778899abc",
            owner_id=42,
            destination="@channel",
            state=JobState.PLANNED,
            items=items,
            created_at="2026-09-04 01:19:23",
        )

    def test_remote_layout_v1_is_human_readable_and_self_describing(self) -> None:
        with TemporaryDirectory() as tmp:
            plan = ArchivePlanner().plan(self._job(Path(tmp), 3))
        package = plan.package
        self.assertEqual(package.layout_version, ARCHIVE_LAYOUT_VERSION)
        self.assertEqual(
            package.remote_path,
            "archive/2026/09/04/20260904-011923__media-003__a84c19d2f0",
        )
        self.assertEqual(package.staging_path, ".staging/arc_a84c19d2f00112233445566778899abc")
        self.assertEqual(
            [obj.remote_relpath for obj in package.objects],
            [
                "media/001__Original 1.jpg",
                "media/002__Original 2.mp4",
                "media/003__Original 3.pdf",
            ],
        )
        self.assertEqual(package.manifest["media_count"], 3)
        self.assertEqual(len(package.manifest_sha256 or ""), 64)
        self.assertNotIn("destination", package.manifest)
        self.assertNotIn("caption", repr(package.manifest))
        tree = ArchivePlanner.render_tree(plan)
        self.assertIn("├── manifest.json", tree)
        self.assertIn("└── _COMPLETE.json", tree)
        self.assertIn("│   ├── 001__Original 1.jpg", tree)

    def test_remote_layout_v2_is_short_dated_sequence(self) -> None:
        with TemporaryDirectory() as tmp:
            job = self._job(Path(tmp), 3)
            plan = ArchivePlanner(remote_root="115/Pron", layout="v2").plan(
                job,
                day="2026-09-13",
                day_seq=2,
            )
        package = plan.package
        self.assertEqual(package.layout_version, "tgvio.archive/v2")
        self.assertEqual(package.remote_path, "115/Pron/2026-09-13/2")
        self.assertEqual(
            package.staging_path,
            "115/Pron/.staging/arc_a84c19d2f00112233445566778899abc",
        )
        expected = [
            f"{item.sha256[:12]}{Path(item.name or '').suffix[:16]}"
            for item in sorted(job.items, key=lambda current: current.index)
        ]
        self.assertEqual([obj.remote_relpath for obj in package.objects], expected)
        self.assertNotIn("media/", repr([obj.remote_relpath for obj in package.objects]))
        tree = ArchivePlanner.render_tree(plan)
        self.assertIn("115/Pron/2026-09-13/2/", tree)

    def test_archive_profile_policy_is_frozen_into_package_and_manifest(self) -> None:
        with TemporaryDirectory() as tmp:
            profile = ArchiveProfileSnapshot(
                profile_id="primary-v2",
                policy=ArchivePolicy.BEST_EFFORT,
                policy_version=2,
            )
            plan = ArchivePlanner(profile=profile).plan(self._job(Path(tmp), 1))
        package = plan.package
        self.assertEqual(package.archive_profile_id, "primary-v2")
        self.assertEqual(package.archive_policy, ArchivePolicy.BEST_EFFORT)
        self.assertEqual(package.archive_policy_version, 2)
        self.assertEqual(
            package.manifest["archive_profile"],
            {"id": "primary-v2", "policy": "best_effort", "policy_version": 2},
        )
        self.assertEqual(plan.summary["archive_profile_id"], "primary-v2")
        self.assertEqual(plan.summary["archive_policy"], "best_effort")

    def test_archive_profile_id_rejects_unsafe_or_secret_like_freeform_values(self) -> None:
        for value in ("", "../other", "profile/name", "profile name"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ArchiveProfileSnapshot(profile_id=value)

    def test_1_10_52_100_media_all_form_one_package_without_album_limit(self) -> None:
        for count in (1, 10, 52, 100):
            with self.subTest(count=count), TemporaryDirectory() as tmp:
                plan = ArchivePlanner().plan(self._job(Path(tmp), count))
                self.assertEqual(len(plan.package.objects), count)
                self.assertEqual(plan.summary["media_total"], count)
                self.assertIn(f"__media-{count:03d}__", plan.package.remote_path)
                self.assertEqual(
                    plan.package.objects[-1].remote_relpath.split("/", 1)[1][:5],
                    f"{count:03d}__",
                )

    def test_only_canonical_media_is_archived_not_telegram_transport_artifacts(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            canonical = root / "canonical.mp4"
            source.write_bytes(b"source")
            canonical.write_bytes(b"canonical")
            (root / "cover.jpg").write_bytes(b"cover")
            (root / "source.part001.mp4").write_bytes(b"segment")
            job = Job(
                id="1" * 32,
                owner_id=42,
                destination="@channel",
                state=JobState.PLANNED,
                created_at="2026-09-04 01:19:23",
                items=[
                    MediaItem(
                        index=0,
                        kind=MediaKind.VIDEO,
                        source="fixture",
                        local_path=str(source),
                        name="movie.mp4",
                        sha256="a" * 64,
                        metadata={"canonical_path": str(canonical)},
                    )
                ],
            )
            plan = ArchivePlanner().plan(job)
        self.assertEqual(len(plan.package.objects), 1)
        self.assertEqual(plan.package.objects[0].local_path, str(canonical))
        preview = ArchivePlanner.render_tree(plan)
        self.assertNotIn("cover.jpg", preview)
        self.assertNotIn("part001", preview)

    def test_missing_hash_or_canonical_file_fails_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "video.mp4"
            path.write_bytes(b"video")
            job = Job(
                id="2" * 32,
                owner_id=42,
                destination="@channel",
                state=JobState.PLANNED,
                items=[
                    MediaItem(
                        index=0,
                        kind=MediaKind.VIDEO,
                        source="fixture",
                        local_path=str(path),
                    )
                ],
            )
            with self.assertRaisesRegex(ArchivePlanningError, "sha256"):
                ArchivePlanner().plan(job)
            job.items[0] = MediaItem(
                index=0,
                kind=MediaKind.VIDEO,
                source="fixture",
                local_path=str(root / "missing.mp4"),
                sha256="b" * 64,
            )
            with self.assertRaisesRegex(ArchivePlanningError, "canonical media missing"):
                ArchivePlanner().plan(job)

    def test_duplicate_content_gets_unique_remote_relpaths(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = b"same-content"
            items = []
            for index in range(2):
                path = root / f"copy-{index}.jpg"
                path.write_bytes(payload)
                items.append(
                    MediaItem(
                        index=index,
                        kind=MediaKind.PHOTO,
                        source=f"fixture:{index}",
                        local_path=str(path),
                        name="same.jpg",
                        size_bytes=len(payload),
                        mime_type="image/jpeg",
                        sha256="a" * 64,
                    )
                )
            job = Job(
                id="dup" + "0" * 29,
                owner_id=1,
                destination="@channel",
                state=JobState.PLANNED,
                items=items,
                created_at="2026-09-04 01:19:23",
            )
            plan = ArchivePlanner(remote_root="115/Pron", layout="v2").plan(
                job,
                day="2026-09-13",
                day_seq=1,
            )
        relpaths = [obj.remote_relpath for obj in plan.package.objects]
        self.assertEqual(len(relpaths), 2)
        self.assertEqual(len(set(relpaths)), 2)
        self.assertEqual(relpaths[0], "a" * 12 + ".jpg")
        self.assertEqual(relpaths[1], "a" * 12 + "-2.jpg")
        original_names = [
            entry["original_name"] for entry in plan.package.manifest["media"]
        ]
        self.assertEqual(original_names, ["same.jpg", "same.jpg"])

    def test_remote_root_is_a_layout_prefix_not_a_second_package_identity(self) -> None:
        with TemporaryDirectory() as tmp:
            job = self._job(Path(tmp), 1)
            plan = ArchivePlanner(remote_root="TGVIO/Media").plan(job)
        self.assertTrue(plan.package.remote_path.startswith("TGVIO/Media/archive/2026/09/04/"))
        self.assertEqual(
            plan.package.staging_path,
            f"TGVIO/Media/.staging/{plan.package.id}",
        )
        with self.assertRaisesRegex(ValueError, "unsafe archive remote root"):
            ArchivePlanner(remote_root="../escape")
