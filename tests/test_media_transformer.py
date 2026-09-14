from __future__ import annotations

import inspect
from pathlib import Path
import unittest
from unittest.mock import AsyncMock

from tgvio.infrastructure.media_transformer import FFmpegMediaTransformer


class ThumbnailPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_blank_frame_rejects_black_or_white(self) -> None:
        transformer = FFmpegMediaTransformer()
        transformer._frame_is_black = AsyncMock(return_value=True)
        transformer._frame_is_white = AsyncMock(return_value=False)
        self.assertTrue(await transformer._frame_is_blank(Path("frame.jpg")))

        transformer._frame_is_black = AsyncMock(return_value=False)
        transformer._frame_is_white = AsyncMock(return_value=True)
        self.assertTrue(await transformer._frame_is_blank(Path("frame.jpg")))

        transformer._frame_is_black = AsyncMock(return_value=False)
        transformer._frame_is_white = AsyncMock(return_value=False)
        self.assertFalse(await transformer._frame_is_blank(Path("frame.jpg")))

    def test_thumbnail_byte_budget_allows_up_to_one_megabyte(self) -> None:
        signature = inspect.signature(FFmpegMediaTransformer.make_video_thumbnail)
        self.assertEqual(signature.parameters["max_bytes"].default, 1_000_000)

    def test_cover_and_thumbnail_sample_multiple_positions(self) -> None:
        self.assertGreaterEqual(len(FFmpegMediaTransformer._cover_seeks(100.0)), 6)


if __name__ == "__main__":
    unittest.main()
