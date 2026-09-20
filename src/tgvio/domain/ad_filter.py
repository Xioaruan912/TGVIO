"""Confidence scoring that spots the advertising rows these source bots inject.

Calibrated against the production picks: real content always arrives as an album
or a large lone video, while the ads were lone photos whose identical content
kept coming back. Caption keywords alone are useless - the sources append a
promotional footer to real posts - so captions only count when the *same*
caption repeats with the *same* file characteristics, and a caption seen with
three or more different files is treated as a footer and ignored entirely.
"""

from __future__ import annotations

from dataclasses import dataclass

from tgvio.domain.job import MediaKind

AD_THRESHOLD = 60
FOOTER_DISTINCT_FILES = 3


@dataclass(frozen=True, slots=True)
class AdVerdict:
    is_ad: bool
    score: int = 0
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AdSignals:
    """Cheap facts about one pick row plus its repetition counts."""

    item_count: int
    kinds: tuple[MediaKind, ...]
    fingerprint: str
    fingerprint_repeats: int = 1
    file_repeats: int = 1
    caption_files: int = 1
    learned: bool = False
    released: bool = False


def normalize_caption(caption: str | None) -> str:
    text = " ".join(str(caption or "").split())
    return text[:200].casefold()


def content_fingerprint(
    *,
    kinds: tuple[MediaKind, ...],
    size_bytes: int,
    width: int | None,
    height: int | None,
    caption: str | None,
) -> str:
    """A download-free identity for "the same file was sent again"."""

    kind = kinds[0].value if len(kinds) == 1 else "mixed"
    return f"{kind}|{int(size_bytes)}|{int(width or 0)}x{int(height or 0)}|{normalize_caption(caption)}"


def ad_verdict(signals: AdSignals) -> AdVerdict:
    """Score one pick row; only lone items can ever be advertisements."""

    if signals.released:
        return AdVerdict(False, 0, ("released",))
    lone = int(signals.item_count) == 1
    if not lone:
        # Albums are content; the sources attach their promotional footer there.
        return AdVerdict(False, 0)
    score = 0
    reasons: list[str] = []
    is_photo = tuple(signals.kinds) == (MediaKind.PHOTO,)
    if is_photo:
        score += 40
        reasons.append("lone_photo")
    repeats = int(signals.fingerprint_repeats)
    # The same content coming back is the strongest signal we have. A lone photo
    # already carries the +40 base, so an isolated video/document needs the full
    # weight to cross the threshold on re-posts alone.
    content_x3_weight = 40 if is_photo else 60
    if repeats >= 3:
        score += content_x3_weight
        reasons.append("same_content_x3")
    elif repeats >= 2:
        score += 25
        reasons.append("same_content_x2")
    file_repeats = int(signals.file_repeats)
    if file_repeats >= 4:
        score += 60
        reasons.append("same_file_x4")
    elif file_repeats >= 2:
        score += 25
        reasons.append("same_file_x2")
    if signals.caption_files >= FOOTER_DISTINCT_FILES:
        # A caption shared by many different files is a footer, not an ad.
        for marker, weight in (
            ("same_content_x2", 25),
            ("same_content_x3", content_x3_weight),
        ):
            if marker in reasons:
                reasons.remove(marker)
                score -= weight
        reasons.append("caption_footer")
    if signals.learned:
        score += 45
        reasons.append("learned_ad")
    return AdVerdict(score >= AD_THRESHOLD, max(0, score), tuple(reasons))


def reason_label(reasons: tuple[str, ...]) -> str:
    """Short Chinese explanation for the hidden-items page."""

    labels = {
        "lone_photo": "孤立图片",
        "same_content_x2": "同一内容出现 2 次",
        "same_content_x3": "同一内容出现 3 次以上",
        "same_file_x2": "同一文件出现 2 次",
        "same_file_x4": "同一文件出现 4 次以上",
        "learned_ad": "之前判过广告",
        "caption_footer": "文案是页脚（不计分）",
        "released": "已放行",
    }
    return " · ".join(labels.get(reason, reason) for reason in reasons)
