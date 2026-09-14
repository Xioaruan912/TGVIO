from __future__ import annotations

from telethon import Button

from tgvio.adapters.telegram.bot_ui_support import *  # noqa: F401,F403
from tgvio.application.publish_styles import (
    BUILTIN_STYLES,
    DEFAULT_STYLE,
    PublishStyleService,
    STYLE_ORDER,
    resolve_style,
)


class BotUIStylesMixin:
    def _style_service(self) -> PublishStyleService:
        return PublishStyleService(self._repository)

    async def _render_styles(self, owner_id: int) -> tuple[str, list]:
        preference = await self._repository.get_user_preference(int(owner_id))
        name, policy = resolve_style(preference.style_json)
        active = BUILTIN_STYLES.get(name, {}).get("label", "自定义")
        lines = [
            "🎨 **发布风格**",
            "──────────",
            f"当前：`{active}`（封面={"开" if policy['cover_mode'] else "关"} · "
            f"原文字={"保留" if policy['forward_caption'] else "不保留"}）",
            "──────────",
        ]
        rows: list[list] = []
        for key in STYLE_ORDER:
            style = BUILTIN_STYLES[key]
            mark = "• " if key == name else ""
            lines.append(f"{mark}**{style['label']}**：{style['description']}")
            rows.append(
                [
                    Button.inline(
                        f"{'✓ ' if key == name else ''}{style['label']}",
                        f"ui:style:{key}".encode("utf-8"),
                    )
                ]
            )
        lines.append("──────────")
        lines.append("风格只改变封面/原文字；不会静默关闭雪花遮挡。确认时冻结到该任务。")
        rows.append(
            [
                Button.inline(
                    f"封面 {'开' if policy['cover_mode'] else '关'}",
                    b"ui:style-custom:cover_mode",
                ),
                Button.inline(
                    f"原文字 {'保留' if policy['forward_caption'] else '不保留'}",
                    b"ui:style-custom:forward_caption",
                ),
            ]
        )
        rows.append(
            [
                Button.inline("↩️ 恢复默认", b"ui:style-reset"),
                Button.inline("🔄 刷新", b"ui:styles"),
            ]
        )
        rows.append([Button.inline("🏠 首页", b"ui:home"), Button.inline("📋 我的任务", b"ui:jobs")])
        return "\n".join(lines), rows

    async def _show_styles_callback(self, event, owner_id: int, _token: str = "") -> None:
        text, rows = await self._render_styles(owner_id)
        await self._edit_page(event, text, rows)

    async def _set_style_callback(self, event, owner_id: int, name: str) -> None:
        try:
            await self._style_service().set_named(owner_id, name)
        except ValueError:
            await self._safe_answer(event, "未知风格", alert=True)
            return
        await self._safe_answer(event, "已更新发布风格")
        text, rows = await self._render_styles(owner_id)
        await self._edit_page(event, text, rows)

    async def _style_custom_callback(self, event, owner_id: int, field: str) -> None:
        policy = await self._style_service().current(owner_id)
        if field == "cover_mode":
            policy = {"cover_mode": not bool(policy["cover_mode"]), "forward_caption": bool(policy["forward_caption"])}
        elif field == "forward_caption":
            policy = {"cover_mode": bool(policy["cover_mode"]), "forward_caption": not bool(policy["forward_caption"])}
        else:
            await self._safe_answer(event, "未知选项", alert=True)
            return
        await self._style_service().set_custom(owner_id, policy=policy)
        await self._safe_answer(event, "已保存我的常用")
        text, rows = await self._render_styles(owner_id)
        await self._edit_page(event, text, rows)

    async def _style_reset_callback(self, event, owner_id: int, _token: str = "") -> None:
        await self._style_service().reset(owner_id)
        await self._safe_answer(event, "已恢复默认风格")
        text, rows = await self._render_styles(owner_id)
        await self._edit_page(event, text, rows)
