"""The local cover mirror: a content-addressed store with a bounded budget.

The mirror is a pure cache. Nothing here decides whether a cover may be served; the cover
route owns that. What this module owns is the invariant that matters: a file under the
mirror's own name is complete, and a key that is not a content hash is not a key.
"""
from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tgvio_player.infrastructure.cover_mirror import CoverMirror, CoverMirrorCounters

DIGEST = "a" * 64
RELPATH = f"cover/backfill/{DIGEST}.jpg"


class CoverMirrorStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "covers"

    def test_only_a_content_addressed_name_is_a_key(self) -> None:
        mirror = CoverMirror(self.root, budget_bytes=1024)
        self.assertEqual(mirror.key_for(RELPATH), DIGEST)
        for bad in [
            "cover/backfill/cover.jpg",
            f"cover/backfill/{DIGEST.upper()}.jpg",
            f"cover/backfill/{'a' * 63}.jpg",
            "cover/backfill/../../etc/passwd",
            "",
        ]:
            self.assertIsNone(mirror.key_for(bad), bad)

    def test_a_hit_needs_the_declared_size(self) -> None:
        mirror = CoverMirror(self.root, budget_bytes=1024)
        mirror.write(DIGEST, b"jpeg-bytes")
        self.assertTrue(mirror.has(DIGEST, 10))
        self.assertFalse(mirror.has(DIGEST, 11), "a size the catalog does not declare is not a hit")
        self.assertFalse(mirror.has("b" * 64, 10), "a missing key is never a hit")

    def test_a_failed_rename_leaves_no_servable_file(self) -> None:
        mirror = CoverMirror(self.root, budget_bytes=1024)
        with patch("tgvio_player.infrastructure.cover_mirror.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                mirror.write(DIGEST, b"jpeg-bytes")
        self.assertFalse(mirror.exists(DIGEST), "a half-written cover must never be servable")
        self.assertEqual([p.name for p in self.root.iterdir() if p.suffix == ".jpg"], [])

    def test_the_sweep_drops_orphans_then_the_oldest_over_budget(self) -> None:
        mirror = CoverMirror(self.root, budget_bytes=30)
        keep, orphan, fresh = "a" * 64, "b" * 64, "c" * 64
        mirror.write(keep, b"x" * 10)
        mirror.write(orphan, b"y" * 10)
        os.utime(mirror.path_for(keep), (1000, 1000))
        self.assertEqual(mirror.sweep({keep}), 1, "an orphan goes first")
        self.assertTrue(mirror.exists(keep))
        mirror.write(fresh, b"z" * 30)
        self.assertEqual(mirror.sweep({keep, fresh}), 1, "over budget, the oldest goes")
        self.assertFalse(mirror.exists(keep))
        self.assertTrue(mirror.exists(fresh))

    def test_stats_counts_only_mirror_files(self) -> None:
        mirror = CoverMirror(self.root, budget_bytes=1024)
        mirror.write(DIGEST, b"x" * 7)
        (self.root / "notes.txt").write_text("not a cover", encoding="utf-8")
        self.assertEqual(mirror.stats(), (1, 7))

    def test_read_returns_the_bytes_or_nothing(self) -> None:
        mirror = CoverMirror(self.root, budget_bytes=1024)
        self.assertIsNone(mirror.read(DIGEST), "an absent key reads as nothing, not as an error")
        mirror.write(DIGEST, b"jpeg-bytes")
        self.assertEqual(mirror.read(DIGEST), b"jpeg-bytes")

    def test_the_counters_start_empty(self) -> None:
        counters = CoverMirrorCounters()
        self.assertEqual(
            (counters.hits, counters.misses, counters.write_failed, counters.warm_pending, counters.warm_failed),
            (0, 0, 0, 0, 0),
        )
