from __future__ import annotations

from pathlib import Path

from telethon import Button

from tgvio.adapters.telegram.bot_ui_support import *  # noqa: F401,F403
from tgvio.application.content_prefs import ContentPreferenceService
from tgvio.domain.content import (
    AUDIO_ONLY_PRESET,
    ContentPreferenceError,
    DEFAULT_YTDLP_PRESET,
    MAX_CAPTION_TEMPLATE_CHARS,
    TEMPLATE_VARIABLES,
    YTDLP_PRESETS,
    YTDLP_PRESET_LABELS,
    normalize_ytdlp_preset,
    validate_caption_template,
)

MAX_THUMBNAIL_BYTES = 5 * 1024 * 1024


class BotUIContentMixin:
    """Owner-scoped content pages: custom thumbnail, caption template, yt-dlp."""

    def _content_service(self) -> ContentPreferenceService:
        return ContentPreferenceService(self._repository)

    async def _content_page(self, owner_id: int) -> tuple[str, list]:
        preference = await self._content_service().current(int(owner_id))
        thumbnail = "已设置" if preference.thumbnail_path else "未设置"
        try:
            preset = normalize_ytdlp_preset(preference.ytdlp_preset)
        except ContentPreferenceError:
            preset = DEFAULT_YTDLP_PRESET
        preset_label = YTDLP_PRESET_LABELS.get(preset, preset)
        audio_label = "开" if preference.ytdlp_audio_only else "关"
        template = (preference.caption_template or "").strip()
        template_line = template if template else "未设置"
        if len(template_line) > 120:
            template_line = template_line[:120] + "…"
        cookies = "已配置" if getattr(self._settings, "ytdlp_cookies_file", "") else "未配置"
        lines = [
            "🧩 **内容与下载**",
            "──────────",
            f"🖼 自定义缩略图：`{thumbnail}`",
            f"🎚 链接画质：`{preset_label}` · 仅音频：`{audio_label}`",
            f"🍪 链接 Cookie：`{cookies}`（服务器配置，需重启）",
            "──────────",
            "📝 当前配文模板：",
            f"{template_line}",
            "──────────",
            "只影响**新接受的任务**：设置会冻结到任务，已在队列/发布中的任务不改变。",
        ]
        rows = [
            [
                Button.inline("🖼 缩略图说明", b"ui:content-thumb-help"),
                Button.inline("🗑 移除缩略图", b"ui:content-thumb-remove"),
            ],
            [
                Button.inline(
                    f"{'✓ ' if preset == key else ''}{YTDLP_PRESET_LABELS[key]}",
                    f"ui:content-ytdlp:{key}".encode("utf-8"),
                )
                for key in YTDLP_PRESETS
            ][:2],
            [
                Button.inline(
                    f"{'✓ ' if preset == key else ''}{YTDLP_PRESET_LABELS[key]}",
                    f"ui:content-ytdlp:{key}".encode("utf-8"),
                )
                for key in list(YTDLP_PRESETS)[2:]
            ]
            + [
                Button.inline(
                    f"🎵 仅音频 {'开' if preference.ytdlp_audio_only else '关'}",
                    b"ui:content-audio-toggle",
                )
            ],
            [
                Button.inline("📝 配文模板说明", b"ui:content-template-help"),
                Button.inline("🗑 清空模板", b"ui:content-template-clear"),
            ],
            [Button.inline("🔄 刷新", b"ui:content"), Button.inline("🏠 首页", b"ui:home")],
        ]
        return "\n".join(lines), rows

    async def _show_content_callback(self, event, owner_id: int, _token: str = "") -> None:
        text, rows = await self._content_page(owner_id)
        await self._edit_page(event, text, rows)

    async def _handle_content_callback(self, event, owner_id: int, action: str) -> bool:
        """Dispatch the content sub-page callbacks; returns True when handled."""

        if action == "ui:content-thumb-help":
            await self._content_thumbnail_help_callback(event)
            return True
        if action == "ui:content-template-help":
            await self._content_template_help_callback(event)
            return True
        if action == "ui:content-thumb-remove":
            await self._content_thumbnail_remove_callback(event, owner_id)
            return True
        if action == "ui:content-template-clear":
            await self._content_template_clear_callback(event, owner_id)
            return True
        if action == "ui:content-audio-toggle":
            await self._content_audio_toggle_callback(event, owner_id)
            return True
        if action.startswith("ui:content-ytdlp:"):
            await self._content_ytdlp_callback(event, owner_id, action.split(":", 2)[2])
            return True
        return False

    async def _content_thumbnail_help_callback(self, event) -> None:
        await self._edit_page(
            event,
            "🖼 **自定义缩略图**\n"
            "──────────\n"
            "用法：**回复任意一张图片**发送 `/thumb`，机器人会把那张图作为新任务里视频的缩略图。\n"
            "• 图片建议为 JPG/PNG，不超过 5 MB；发布前会自动压到 320px 以内。\n"
            "• 未设置时使用自动截帧；电影/剧集用统一封面时更整齐。\n"
            "• 想恢复自动截帧：点「🗑 移除缩略图」。",
            [
                [Button.inline("⬅️ 返回", b"ui:content")],
                [Button.inline("🏠 首页", b"ui:home")],
            ],
        )

    async def _content_template_help_callback(self, event) -> None:
        variables = " ".join(f"`{{{name}}}`" for name in TEMPLATE_VARIABLES)
        await self._edit_page(
            event,
            "📝 **配文模板**\n"
            "──────────\n"
            "设置：发送 `/caption 模板内容`，或**回复一条包含模板的消息**发送 `/caption`。\n"
            f"可用变量：{variables}\n"
            "例：\n"
            "`{channel} | {date} 第 {index} 集`\n"
            "按钮：单独一行写 `button: 文字 | https://链接`（只对单条媒体生效）。\n"
            f"• 最长 {MAX_CAPTION_TEMPLATE_CHARS} 字符；未知变量会被拒绝。\n"
            "• 模板会追加在原有文案之后；清空即恢复默认。",
            [
                [Button.inline("⬅️ 返回", b"ui:content")],
                [Button.inline("🏠 首页", b"ui:home")],
            ],
        )

    async def _content_thumbnail_remove_callback(self, event, owner_id: int) -> None:
        await self._content_service().clear_thumbnail(owner_id)
        await self._safe_answer(event, "已移除缩略图")
        text, rows = await self._content_page(owner_id)
        await self._edit_page(event, text, rows)

    async def _content_template_clear_callback(self, event, owner_id: int) -> None:
        await self._content_service().clear_caption_template(owner_id)
        await self._safe_answer(event, "已清空模板")
        text, rows = await self._content_page(owner_id)
        await self._edit_page(event, text, rows)

    async def _content_ytdlp_callback(self, event, owner_id: int, preset: str) -> None:
        try:
            await self._content_service().set_ytdlp(owner_id, preset=preset)
        except ContentPreferenceError:
            await self._safe_answer(event, "未知画质", alert=True)
            return
        await self._safe_answer(event, "已更新链接画质")
        text, rows = await self._content_page(owner_id)
        await self._edit_page(event, text, rows)

    async def _content_audio_toggle_callback(self, event, owner_id: int) -> None:
        preference = await self._content_service().current(owner_id)
        await self._content_service().set_ytdlp(
            owner_id,
            audio_only=not bool(preference.ytdlp_audio_only),
        )
        await self._safe_answer(event, "已更新")
        text, rows = await self._content_page(owner_id)
        await self._edit_page(event, text, rows)

    async def _set_thumbnail_from_message(self, event, owner_id: int) -> None:
        reply = await event.get_reply_message()
        if reply is None:
            await event.respond(
                "请**回复一张图片**再发送 `/thumb`。",
                parse_mode="md",
            )
            return
        if getattr(reply, "photo", None) is None and getattr(reply, "document", None) is None:
            await event.respond("回复的消息里没有图片，请回复图片后重试。")
            return
        target_dir = Path(self._settings.data_dir) / "content"
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            await event.respond("无法创建缩略图目录，请查看日志。")
            return
        target = target_dir / f"thumbnail-{int(owner_id)}.jpg"
        try:
            downloaded = await self._client.download_media(reply, file=str(target))
        except Exception:
            await event.respond("下载图片失败，请稍后重试。")
            return
        path = Path(downloaded) if downloaded else target
        if not path.is_file() or path.stat().st_size == 0:
            await event.respond("图片内容为空，请换一张重试。")
            return
        if path.stat().st_size > MAX_THUMBNAIL_BYTES:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            await event.respond("图片超过 5 MB，请换一张更小的图片。")
            return
        try:
            await self._content_service().set_thumbnail(owner_id, str(path))
        except ContentPreferenceError:
            await event.respond("缩略图保存失败。")
            return
        await event.respond("已设置自定义缩略图：新任务的视频会使用它。")

    async def _set_caption_template_from_message(
        self,
        event,
        owner_id: int,
        argument: str,
    ) -> None:
        reply = await event.get_reply_message()
        text = ""
        if reply is not None and (reply.raw_text or "").strip():
            text = (reply.raw_text or "").strip()
        elif argument.strip():
            text = argument.strip().replace("\\n", "\n")
        if not text:
            variables = " ".join(f"`{{{name}}}`" for name in TEMPLATE_VARIABLES)
            await event.respond(
                "📝 **配文模板**\n"
                "用法：`/caption 模板内容`，或回复一条消息发送 `/caption`。\n"
                f"可用变量：{variables}",
                parse_mode="md",
            )
            return
        try:
            await self._content_service().set_caption_template(owner_id, text)
        except ContentPreferenceError as exc:
            await event.respond(f"模板无效：{exc}")
            return
        await event.respond("已更新配文模板：新任务发布会上追加该文案。")
