from __future__ import annotations

import unittest

from tgvio.infrastructure.cover_frames import dhash_gray32 as container_hash, flat_extreme_frame as container_flat
from tgvio_player.domain.cover_hash import dhash_gray32, flat_extreme_frame


class CoverHashTests(unittest.TestCase):
    def test_the_player_hashes_covers_exactly_like_the_container_did(self) -> None:
        for seed in range(20):
            gray = bytes((x * 7 + y * 13 + seed * 31) % 256 for y in range(32) for x in range(32))
            self.assertEqual(dhash_gray32(gray), container_hash(gray))
            self.assertEqual(flat_extreme_frame(gray), container_flat(gray))

    def test_black_frames_are_flat_and_wrong_sizes_refused(self) -> None:
        self.assertTrue(flat_extreme_frame(bytes(1024)))
        with self.assertRaises(ValueError):
            dhash_gray32(bytes(10))


if __name__ == "__main__":
    unittest.main()
