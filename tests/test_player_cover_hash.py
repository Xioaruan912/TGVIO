from __future__ import annotations

import unittest

from tgvio_player.domain.cover_hash import dhash_gray32, flat_extreme_frame


class CoverHashTests(unittest.TestCase):
    def test_the_player_hashes_covers_exactly_like_the_retired_container_did(self) -> None:
        # Golden values recorded from tgvio.infrastructure.cover_frames before it was removed.
        golden = [('00030c30c000030c', False), ('010638e080030e38', False), ('031c70c001071c70', False), ('0e38e000030e38c0', False), ('1c708001071c6080', False), ('30c000030c30c001', False), ('e080010e38e08003', False), ('c001071c70c00107', False), ('80030e38c000030e', False), ('01071c6080010718', False), ('030c30c000030c70', False), ('0e38e080030e38e0', False), ('1c70c001071c70c0', False), ('38e000030e30c000', False), ('7080010718e08001', False), ('c000030c70c00103', False), ('80030e38e080030e', False), ('01071c70c001071c', False), ('030e38c000030c30', False), ('071c6080010638e0', False)]
        for seed, (hash_value, flat) in enumerate(golden):
            gray = bytes((x * 7 + y * 13 + seed * 31) % 256 for y in range(32) for x in range(32))
            self.assertEqual(dhash_gray32(gray), hash_value)
            self.assertEqual(flat_extreme_frame(gray), flat)

    def test_black_frames_are_flat_and_wrong_sizes_refused(self) -> None:
        self.assertTrue(flat_extreme_frame(bytes(1024)))
        with self.assertRaises(ValueError):
            dhash_gray32(bytes(10))


if __name__ == "__main__":
    unittest.main()
