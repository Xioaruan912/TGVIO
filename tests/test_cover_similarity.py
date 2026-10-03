from __future__ import annotations

import unittest

from tgvio_player.infrastructure.cover_similarity import (
    DUPLICATE_DISTANCE,
    SIMILAR_DISTANCE,
    hamming,
    is_fingerprint,
    nearest_fingerprints,
)


class CoverSimilarityTests(unittest.TestCase):
    """Distance is a pure function of two readable fingerprints, and nothing else."""

    def test_distance_is_a_popcount_and_unreadable_values_are_rejected(self) -> None:
        self.assertEqual(hamming("0000000000000000", "ffffffffffffffff"), 64)
        self.assertEqual(hamming("0000000000000000", "8000000000000000"), 1)
        for bad in (None, "", "0123456789abcdef0", "0123456789ABCDEF", "g123456789abcdef", 7):
            self.assertFalse(is_fingerprint(bad), bad)
            with self.assertRaises(ValueError):
                hamming("0000000000000000", bad)  # type: ignore[arg-type]

    def test_the_bands_are_the_ones_the_product_promised(self) -> None:
        self.assertEqual(DUPLICATE_DISTANCE, 6)
        self.assertEqual(SIMILAR_DISTANCE, 16)

    def test_nearest_skips_the_unreadable_dedupes_and_breaks_ties_by_id(self) -> None:
        rows = [
            ("b", "000000000000000f"),      # distance 4
            ("a", "000000000000000f"),      # distance 4, same band, lower id first
            ("b", "0000000000000001"),      # distance 1: this media keeps its nearest
            ("c", None),                    # unreadable: skipped, never compared
            ("d", "ffffffffffffffff"),      # distance 64: outside any band
        ]
        self.assertEqual(
            nearest_fingerprints(rows, "0000000000000000", threshold=SIMILAR_DISTANCE, limit=10),
            (("b", 1), ("a", 4)),
        )
        self.assertEqual(
            nearest_fingerprints(rows, "0000000000000000", threshold=2, limit=10), (("b", 1),)
        )
        self.assertEqual(nearest_fingerprints(rows, "nonsense", threshold=16, limit=10), ())

    def test_limit_is_bounded(self) -> None:
        rows = [(str(index), "0000000000000000") for index in range(5)]
        self.assertEqual(
            nearest_fingerprints(rows, "0000000000000000", threshold=0, limit=2),
            (("0", 0), ("1", 0)),
        )


if __name__ == "__main__":
    unittest.main()
