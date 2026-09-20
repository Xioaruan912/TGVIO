from __future__ import annotations

import unittest

from tgvio.domain.ad_filter import (
    AD_THRESHOLD,
    AdSignals,
    ad_verdict,
    content_fingerprint,
    normalize_caption,
    reason_label,
)
from tgvio.domain.job import MediaKind

PHOTO = (MediaKind.PHOTO,)
VIDEO = (MediaKind.VIDEO,)


def _signals(**overrides) -> AdSignals:
    values = {
        "item_count": 1,
        "kinds": PHOTO,
        "fingerprint": "photo|116647|720x520|ad",
        "fingerprint_repeats": 1,
        "file_repeats": 1,
        "caption_files": 1,
        "learned": False,
        "released": False,
    }
    values.update(overrides)
    return AdSignals(**values)


class AdVerdictTests(unittest.TestCase):
    def test_lone_photo_repeated_is_an_ad(self) -> None:
        verdict = ad_verdict(_signals(fingerprint_repeats=2))
        self.assertTrue(verdict.is_ad)
        self.assertGreaterEqual(verdict.score, AD_THRESHOLD)
        self.assertIn("lone_photo", verdict.reasons)
        self.assertIn("same_content_x2", verdict.reasons)

    def test_bare_lone_photo_stays_visible(self) -> None:
        verdict = ad_verdict(_signals())
        self.assertFalse(verdict.is_ad)
        self.assertEqual(verdict.score, 40)

    def test_album_with_promotional_footer_is_never_an_ad(self) -> None:
        # Production: the same footer caption appears on 22 real content posts.
        verdict = ad_verdict(
            _signals(
                item_count=10,
                kinds=(MediaKind.PHOTO, MediaKind.VIDEO),
                fingerprint_repeats=22,
                file_repeats=1,
                caption_files=1,
            )
        )
        self.assertFalse(verdict.is_ad)
        self.assertEqual(verdict.score, 0)

    def test_footer_caption_is_ignored_for_a_lone_photo(self) -> None:
        verdict = ad_verdict(_signals(fingerprint_repeats=3, caption_files=5))
        self.assertNotIn("same_content_x3", verdict.reasons)
        self.assertIn("caption_footer", verdict.reasons)
        self.assertFalse(verdict.is_ad)

    def test_lone_video_repeated_many_times_is_an_ad(self) -> None:
        verdict = ad_verdict(_signals(kinds=VIDEO, file_repeats=4))
        self.assertTrue(verdict.is_ad)
        self.assertIn("same_file_x4", verdict.reasons)

    def test_rerereposted_identical_video_is_an_ad(self) -> None:
        # Different file ids (a re-upload) but identical content: the video needs
        # the full weight to cross the threshold without the photo base score.
        verdict = ad_verdict(_signals(kinds=VIDEO, fingerprint_repeats=3))
        self.assertTrue(verdict.is_ad)
        self.assertIn("same_content_x3", verdict.reasons)

    def test_identical_video_twice_stays_visible(self) -> None:
        verdict = ad_verdict(_signals(kinds=VIDEO, fingerprint_repeats=2))
        self.assertFalse(verdict.is_ad)
        self.assertEqual(verdict.score, 25)

    def test_footer_guard_also_removes_the_video_content_weight(self) -> None:
        verdict = ad_verdict(
            _signals(kinds=VIDEO, fingerprint_repeats=3, caption_files=4)
        )
        self.assertFalse(verdict.is_ad)
        self.assertEqual(verdict.score, 0)
        self.assertIn("caption_footer", verdict.reasons)

    def test_single_large_video_is_not_an_ad(self) -> None:
        verdict = ad_verdict(_signals(kinds=VIDEO))
        self.assertFalse(verdict.is_ad)

    def test_learned_fingerprint_pushes_over_the_threshold(self) -> None:
        verdict = ad_verdict(_signals(learned=True))
        self.assertTrue(verdict.is_ad)
        self.assertIn("learned_ad", verdict.reasons)

    def test_released_fingerprint_wins_over_every_signal(self) -> None:
        verdict = ad_verdict(_signals(fingerprint_repeats=9, file_repeats=9, learned=True, released=True))
        self.assertFalse(verdict.is_ad)
        self.assertEqual(verdict.score, 0)
        self.assertEqual(verdict.reasons, ("released",))

    def test_reason_labels_are_readable(self) -> None:
        self.assertIn("孤立图片", reason_label(("lone_photo",)))
        self.assertIn("之前判过广告", reason_label(("learned_ad",)))


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_ignores_whitespace_and_case(self) -> None:
        first = content_fingerprint(
            kinds=PHOTO, size_bytes=100, width=10, height=20, caption="  Hello   World "
        )
        second = content_fingerprint(
            kinds=PHOTO, size_bytes=100, width=10, height=20, caption="hello world"
        )
        self.assertEqual(first, second)

    def test_fingerprint_changes_with_size(self) -> None:
        small = content_fingerprint(kinds=PHOTO, size_bytes=100, width=10, height=20, caption="x")
        large = content_fingerprint(kinds=PHOTO, size_bytes=200, width=10, height=20, caption="x")
        self.assertNotEqual(small, large)

    def test_normalize_caption_truncates(self) -> None:
        self.assertLessEqual(len(normalize_caption("x" * 500)), 200)


if __name__ == "__main__":
    unittest.main()
