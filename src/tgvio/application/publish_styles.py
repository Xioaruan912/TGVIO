from __future__ import annotations

import json

from tgvio.application.ports import JobRepository


BUILTIN_STYLES: dict[str, dict[str, object]] = {
    "minimal": {
        "label": "极简直发",
        "description": "媒体直接发到频道，不生成封面；不保留原消息文字。",
        "cover_mode": False,
        "forward_caption": False,
    },
    "cover": {
        "label": "封面合集",
        "description": "频道先发封面图，媒体进入评论区线程；不保留原文字。",
        "cover_mode": True,
        "forward_caption": False,
    },
    "photo_text": {
        "label": "图文精选",
        "description": "封面模式并在封面保留原消息文字。",
        "cover_mode": True,
        "forward_caption": True,
    },
}
DEFAULT_STYLE = "cover"
STYLE_ORDER = ("minimal", "cover", "photo_text")


def style_policy(*, cover_mode: bool, forward_caption: bool) -> dict[str, object]:
    return {"cover_mode": bool(cover_mode), "forward_caption": bool(forward_caption)}


def resolve_style(style_json: str | None) -> tuple[str, dict[str, object]]:
    """Return (style label key, effective policy) for a stored preference."""
    data: dict[str, object] = {}
    if style_json:
        try:
            parsed = json.loads(style_json)
            if isinstance(parsed, dict):
                data = parsed
        except (TypeError, ValueError):
            data = {}
    name = str(data.get("style") or "")
    if name in BUILTIN_STYLES:
        builtin = BUILTIN_STYLES[name]
        return name, style_policy(
            cover_mode=bool(builtin["cover_mode"]),
            forward_caption=bool(builtin["forward_caption"]),
        )
    if "cover_mode" in data or "forward_caption" in data:
        return "custom", style_policy(
            cover_mode=bool(data.get("cover_mode", True)),
            forward_caption=bool(data.get("forward_caption", False)),
        )
    builtin = BUILTIN_STYLES[DEFAULT_STYLE]
    return DEFAULT_STYLE, style_policy(
        cover_mode=bool(builtin["cover_mode"]),
        forward_caption=bool(builtin["forward_caption"]),
    )


class PublishStyleService:
    """Owner-scoped publish style presets plus a single-use custom override."""

    def __init__(self, repository: JobRepository) -> None:
        self._repository = repository

    async def current(self, owner_id: int) -> dict[str, object]:
        preference = await self._repository.get_user_preference(int(owner_id))
        _, policy = resolve_style(preference.style_json)
        return policy

    async def current_name(self, owner_id: int) -> str:
        preference = await self._repository.get_user_preference(int(owner_id))
        name, _ = resolve_style(preference.style_json)
        return name

    async def set_named(self, owner_id: int, name: str) -> dict[str, object]:
        if name not in BUILTIN_STYLES:
            raise ValueError("unknown style")
        await self._repository.set_user_style(
            int(owner_id), json.dumps({"style": name}, ensure_ascii=False)
        )
        return await self.current(owner_id)

    async def set_custom(self, owner_id: int, *, policy: dict[str, object]) -> dict[str, object]:
        payload = style_policy(
            cover_mode=bool(policy.get("cover_mode", True)),
            forward_caption=bool(policy.get("forward_caption", False)),
        )
        await self._repository.set_user_style(
            int(owner_id), json.dumps(payload, ensure_ascii=False)
        )
        return payload

    async def reset(self, owner_id: int) -> dict[str, object]:
        await self._repository.set_user_style(int(owner_id), None)
        return await self.current(owner_id)
