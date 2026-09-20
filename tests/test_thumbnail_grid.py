from __future__ import annotations

import shutil
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from tgvio.infrastructure.thumbnail_grid import ThumbnailGridBuilder


class GridArgsTests(unittest.TestCase):
    def test_layout_follows_the_grid_shape(self) -> None:
        builder = ThumbnailGridBuilder(tile=100)
        args = builder.build_args([None] * 10, Path("/tmp/out.jpg"))
        layout = "0_0|100_0|200_0|300_0|400_0|0_100|100_100|200_100|300_100|400_100"
        self.assertIn(f"xstack=inputs=10:layout={layout}[grid]", " ".join(args))
        self.assertEqual(builder.grid_shape(10), (5, 2))
        self.assertEqual(builder.grid_shape(3), (3, 1))
        self.assertEqual(builder.grid_shape(1), (1, 1))
        self.assertEqual(builder.grid_shape(0), (0, 0))

    def test_placeholder_slots_use_a_solid_colour_input(self) -> None:
        builder = ThumbnailGridBuilder(tile=96)
        args = builder.build_args([None, Path("/tmp/a.jpg")], Path("/tmp/out.jpg"))
        self.assertEqual(args.count("-f"), 1)
        self.assertIn("color=c=0x1f1f1f:s=96x96", args)
        self.assertIn("-frames:v", args)

    def test_empty_slot_list_builds_nothing(self) -> None:
        self.assertEqual(ThumbnailGridBuilder().build_args([], Path("/tmp/out.jpg")), [])

    def test_every_tile_is_numbered(self) -> None:
        builder = ThumbnailGridBuilder(tile=320)
        args = builder.build_args([None] * 10, Path("/tmp/out.jpg"))
        joined = " ".join(args)
        self.assertEqual(joined.count("drawtext="), 10)
        for position in range(1, 11):
            self.assertIn(f"text='{position}'", joined)
        self.assertIn("fontcolor=white", joined)
        self.assertIn("boxcolor=black@0.55", joined)
        self.assertIn("DejaVuSans-Bold.ttf", joined)
        self.assertIn("x=10:y=10", joined)

    def test_numbers_scale_with_the_tile(self) -> None:
        small = " ".join(ThumbnailGridBuilder(tile=96).build_args([None], Path("/tmp/o.jpg")))
        large = " ".join(ThumbnailGridBuilder(tile=640).build_args([None], Path("/tmp/o.jpg")))
        self.assertIn("fontsize=18", small)
        self.assertIn("fontsize=64", large)

    def test_numbering_can_be_disabled(self) -> None:
        builder = ThumbnailGridBuilder(tile=320, numbered=False)
        joined = " ".join(builder.build_args([None] * 3, Path("/tmp/out.jpg")))
        self.assertNotIn("drawtext", joined)

    def test_missing_font_falls_back_to_a_plain_grid(self) -> None:
        builder = ThumbnailGridBuilder(fontfile="/tmp/definitely-missing-font.ttf")
        joined = " ".join(builder.build_args([None] * 3, Path("/tmp/out.jpg")))
        self.assertNotIn("drawtext", joined)


class GridBuildTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ffmpeg = shutil.which("ffmpeg")
        if cls.ffmpeg is None:
            raise unittest.SkipTest("ffmpeg is not installed")

    def test_single_slot_does_not_use_xstack(self) -> None:
        builder = ThumbnailGridBuilder(tile=96)
        args = builder.build_args([Path("/tmp/a.jpg")], Path("/tmp/out.jpg"))
        joined = " ".join(args)
        self.assertNotIn("xstack", joined)
        self.assertIn("-map [v0]", joined)

    async def test_real_ffmpeg_builds_a_single_tile(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            tile = root / "one.jpg"
            subprocess.run(
                [
                    self.ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=green:s=80x60",
                    "-frames:v",
                    "1",
                    "-update",
                    "1",
                    str(tile),
                ],
                check=True,
            )
            builder = ThumbnailGridBuilder(tile=96)
            output = root / "single.jpg"
            result = await builder.build([tile], output)
            self.assertEqual(result, output)
            self.assertTrue(output.read_bytes().startswith(b"\xff\xd8"))

    async def test_real_ffmpeg_tiles_thumbnails_and_placeholders(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            tiles = []
            for index, colour in enumerate(("red", "blue")):
                path = root / f"tile-{index}.jpg"
                subprocess.run(
                    [
                        self.ffmpeg,
                        "-y",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-f",
                        "lavfi",
                        "-i",
                        f"color=c={colour}:s=80x60",
                        "-frames:v",
                        "1",
                        "-update",
                        "1",
                        str(path),
                    ],
                    check=True,
                )
                tiles.append(path)
            builder = ThumbnailGridBuilder(tile=64, columns=3, max_bytes=1024 * 1024)
            output = root / "grid.jpg"
            result = await builder.build([tiles[0], None, tiles[1]], output)
            self.assertEqual(result, output)
            payload = output.read_bytes()
            self.assertTrue(payload.startswith(b"\xff\xd8"), payload[:4])
            self.assertLessEqual(len(payload), 1024 * 1024)

    async def test_unusable_slots_yield_no_image(self) -> None:
        builder = ThumbnailGridBuilder()
        self.assertIsNone(await builder.build([], Path("/tmp/never.jpg")))

    async def _tile(self, root: Path, name: str, colour: str) -> Path:
        path = root / name
        subprocess.run(
            [
                self.ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c={colour}:s=120x90",
                "-frames:v",
                "1",
                "-update",
                "1",
                str(path),
            ],
            check=True,
        )
        return path

    async def test_numbers_change_the_rendered_image(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            slots = [
                await self._tile(root, "a.jpg", "red"),
                await self._tile(root, "b.jpg", "blue"),
            ]
            plain = root / "plain.jpg"
            numbered = root / "numbered.jpg"
            builder = ThumbnailGridBuilder(tile=128, columns=2)
            self.assertTrue(await builder._run_once(slots, plain, 3, numbered=False))
            self.assertTrue(await builder._run_once(slots, numbered, 3, numbered=True))
            self.assertNotEqual(plain.read_bytes(), numbered.read_bytes())

    async def test_build_survives_a_missing_font(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            slots = [await self._tile(root, "a.jpg", "green")]
            builder = ThumbnailGridBuilder(
                tile=128, fontfile="/tmp/definitely-missing-font.ttf"
            )
            output = root / "grid.jpg"
            result = await builder.build(slots, output)
            self.assertEqual(result, output)
            self.assertTrue(output.read_bytes().startswith(b"\xff\xd8"))


if __name__ == "__main__":
    unittest.main()
