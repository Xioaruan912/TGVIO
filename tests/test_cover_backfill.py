from __future__ import annotations

import unittest

from tgvio.application.cover_backfill import (
    BACKFILL_ALGORITHM,
    COVERS_SCHEMA,
    BackfillTarget,
    CoverBackfill,
    build_covers_index,
    cover_relpath_for,
    parse_covers_index,
)


class CoverBackfillTests(unittest.IsolatedAsyncioTestCase):
    def _targets(self, count: int) -> list[BackfillTarget]:
        return [
            BackfillTarget(
                media_path=f"media/{index:03d}__clip.mp4",
                source_path=f"package/clip-{index}.mp4",
                item_index=index,
            )
            for index in range(count)
        ]

    async def test_only_a_bounded_frame_becomes_a_cover(self) -> None:
        seen: list[str] = []

        async def frame(source: str) -> bytes | None:
            seen.append(source)
            return b"jpeg-bytes"

        result = await CoverBackfill(frame).run(self._targets(2))
        self.assertEqual([cover.media_path for cover in result.covers], [
            "media/000__clip.mp4",
            "media/001__clip.mp4",
        ])
        self.assertEqual(seen, ["package/clip-0.mp4", "package/clip-1.mp4"])
        self.assertEqual([cover.cover_relpath for cover in result.covers], [
            "cover/000__clip-cover.jpg",
            "cover/001__clip-cover.jpg",
        ])
        self.assertEqual(result.skipped, ())
        self.assertEqual(result.covers[0].index_entry()["mime_type"], "image/jpeg")
        self.assertEqual(result.covers[0].index_entry()["size_bytes"], 10)

    async def test_a_failing_or_unusable_frame_is_skipped_without_raising(self) -> None:
        async def frame(source: str) -> bytes | None:
            if source.endswith("clip-0.mp4"):
                raise RuntimeError("ffmpeg gone")
            if source.endswith("clip-1.mp4"):
                return None
            if source.endswith("clip-2.mp4"):
                return b""
            return b"x" * 1_000_001

        result = await CoverBackfill(frame, max_cover_bytes=1_000_000).run(self._targets(4))
        self.assertEqual(result.covers, ())
        self.assertEqual(len(result.skipped), 4)
        self.assertEqual(result.reason, "nothing_produced")

    async def test_already_covered_items_are_not_sampled_again(self) -> None:
        seen: list[str] = []

        async def frame(source: str) -> bytes | None:
            seen.append(source)
            return b"jpeg"

        result = await CoverBackfill(frame).run(
            self._targets(3), already_covered=["media/001__clip.mp4"]
        )
        self.assertEqual(seen, ["package/clip-0.mp4", "package/clip-2.mp4"])
        self.assertEqual(len(result.covers), 2)

    async def test_the_total_deadline_stops_the_run_and_marks_the_rest(self) -> None:
        now = [0.0]

        async def frame(_source: str) -> bytes | None:
            now[0] += 10.0
            return b"jpeg"

        backfill = CoverBackfill(frame, total_deadline_seconds=15.0, clock=lambda: now[0])
        result = await backfill.run(self._targets(4))
        self.assertEqual(len(result.covers), 2, "the run stops once the deadline passes")
        self.assertEqual(result.skipped, ("media/002__clip.mp4", "media/003__clip.mp4"))
        self.assertEqual(result.reason, "deadline")

    async def test_colliding_names_are_deduplicated(self) -> None:
        taken: set[str] = set()
        first = cover_relpath_for("media/dup.mp4", taken)
        second = cover_relpath_for("media/dup.mov", taken)
        self.assertEqual(first, "cover/dup-cover.jpg")
        self.assertEqual(second, "cover/dup-cover-2.jpg")

    async def test_index_round_trips_and_refuses_junk(self) -> None:
        async def frame(_source: str) -> bytes | None:
            return b"jpeg"

        result = await CoverBackfill(frame).run(self._targets(1))
        index = build_covers_index(result.covers)
        self.assertEqual(index["schema"], COVERS_SCHEMA)
        self.assertEqual(index["algorithm"], BACKFILL_ALGORITHM)
        parsed = parse_covers_index(index)
        self.assertEqual(
            parsed["media/000__clip.mp4"]["path"], "cover/000__clip-cover.jpg"
        )
        self.assertEqual(parse_covers_index(b"not json"), {})
        self.assertEqual(parse_covers_index({"schema": "other", "covers": {}}), {})
        self.assertEqual(
            parse_covers_index({"schema": COVERS_SCHEMA, "covers": {"a.mp4": {"path": "/etc/passwd"}}}),
            {},
            "a cover path outside the sidecar prefix is refused",
        )


if __name__ == "__main__":
    unittest.main()
