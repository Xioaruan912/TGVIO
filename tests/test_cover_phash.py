from __future__ import annotations

import unittest

from tgvio.infrastructure.cover_frames import dhash_gray32


class CoverPhashTests(unittest.TestCase):
    """The fingerprint is a pure function of the 32x32 gray frame the worker already has."""

    def test_a_horizontal_gradient_sets_every_bit(self) -> None:
        # Column c is (31-c)*8, so every box-mean column is strictly darker than the one
        # before it and all 64 comparisons are true. The mirrored frame is the mirror
        # image: every comparison false.
        darker_to_the_right = bytes((31 - index % 32) * 8 for index in range(1024))
        self.assertEqual(dhash_gray32(darker_to_the_right), "ffffffffffffffff")
        brighter_to_the_right = bytes((index % 32) * 8 for index in range(1024))
        self.assertEqual(dhash_gray32(brighter_to_the_right), "0000000000000000")

    def test_one_block_of_contrast_sets_exactly_one_bit(self) -> None:
        # Only the first 3x4 block is brighter, so grid[0][0] > grid[0][1] and every
        # other neighbour pair is equal. Bit 0 is the most significant bit.
        gray = bytearray(128 for _ in range(1024))
        for row in range(4):
            for column in range(3):
                gray[row * 32 + column] = 200
        self.assertEqual(dhash_gray32(bytes(gray)), "8000000000000000")

    def test_the_same_frame_hashes_the_same_way_and_the_length_is_checked(self) -> None:
        gray = bytes(range(256)) * 4
        self.assertEqual(dhash_gray32(gray), dhash_gray32(gray))
        self.assertEqual(len(dhash_gray32(gray)), 16)
        with self.assertRaises(ValueError):
            dhash_gray32(gray[:1023])


if __name__ == "__main__":
    unittest.main()
