from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telethon import Button

from tgvio.adapters.telegram.bot_ui_support import *  # noqa: F401,F403
from tgvio.adapters.telegram.bot_ui_source_ads import (
    _PICK_SCAN_ITEMS,
    BotUISourceAdsMixin,
)
from tgvio.adapters.telegram.bot_ui_source_done import BotUISourceDoneMixin
from tgvio.adapters.telegram.bot_ui_source_pick import BotUISourcePickMixin
from tgvio.adapters.telegram.bot_ui_source_merge import BotUISourceMergeMixin
from tgvio.adapters.telegram.bot_ui_source_preview import BotUISourcePreviewMixin
from tgvio.adapters.telegram.source_runtime import SourceCoordinator, SourceLoginError
from tgvio.domain.job import MediaKind

_LOCAL_TZ = ZoneInfo("Asia/Shanghai")

_KIND_WORDS: dict[MediaKind, str] = {
    MediaKind.PHOTO: "🖼 图片",
    MediaKind.VIDEO: "🎬 视频",
    MediaKind.AUDIO: "🎵 音频",
    MediaKind.DOCUMENT: "📄 文件",
}
_PICK_PAGE_SIZE = 10

_ACTIONS = (
    "ui:source",
    "ui:pick",
    "ui:pr",
    "ui:sp:",
    "ui:sg",
    "ui:sy",
    "ui:sn",
    "ui:sv",
    "ui:pc",
    "ui:pk",
    "ui:ps",
    "ui:pdr",
    "ui:srm",
    "ui:sf",
    "ui:sd",
    "ui:sk",
    "ui:sx",
    "ui:sz",
    "ui:sm",
    "ui:spm",
    "ui:sa",
    "ui:sh",
    "ui:sr",
)


class BotUISourceMixin(
    BotUISourceMergeMixin,
    BotUISourcePickMixin,
    BotUISourceAdsMixin,
    BotUISourcePreviewMixin,
    BotUISourceDoneMixin,
):
    """In-Bot personal-account source setup and visual content picking."""

    def _source_coordinator(self) -> SourceCoordinator | None:
        return getattr(self, "_source", None)

    def _pick_preview_service(self):
        return getattr(self, "_pick_previews", None)

    def _pick_filter(self) -> dict[int, bool]:
        state = getattr(self, "_pick_video_only", None)
        if state is None:
            state = {}
            self._pick_video_only = state
        return state

    def _pick_cache(self) -> dict:
        cache = getattr(self, "_pick_page_cache", None)
        if cache is None:
            cache = {}
            self._pick_page_cache = cache
        return cache

    def _pick_window(self) -> dict[int, str]:
        state = getattr(self, "_pick_windows", None)
        if state is None:
            state = {}
            self._pick_windows = state
        return state

    def _pick_sources(self) -> dict[int, int]:
        state = getattr(self, "_pick_sources_seen", None)
        if state is None:
            state = {}
            self._pick_sources_seen = state
        return state

    def _window_since(self, owner_id: int) -> datetime:
        """Start of the visible window: today, or today plus yesterday."""

        start = datetime.now(_LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        if self._pick_window().get(int(owner_id)) == "2d":
            start -= timedelta(days=1)
        return start

    def _pick_grid_messages(self) -> dict[int, int]:
        state = getattr(self, "_pick_grid_msgs", None)
        if state is None:
            state = {}
            self._pick_grid_msgs = state
        return state

    def _pick_list_messages(self) -> dict[int, int]:
        """Message id of the rendered pick page, so previews can refresh it."""

        state = getattr(self, "_pick_list_msgs", None)
        if state is None:
            state = {}
            self._pick_list_msgs = state
        return state

    def _pick_pages(self) -> dict[int, tuple[int, int]]:
        """Last rendered ``(source_index, page)`` of the pick list."""

        state = getattr(self, "_pick_last_pages", None)
        if state is None:
            state = {}
            self._pick_last_pages = state
        return state

    async def _refresh_list_page(self, owner_id: int, chat_id: int) -> None:
        """Re-render the tracked list message (selection changed from elsewhere)."""

        tracked = self._pick_list_messages().get(int(owner_id))
        last = self._pick_pages().get(int(owner_id))
        if tracked is None or last is None:
            return
        source_index, page = last
        text, rows, _summaries = await self._pick_render(
            owner_id, source_index, page
        )
        await self._edit_text_buttons(chat_id, int(tracked), text, rows)

    async def _edit_text_buttons(self, chat_id: int, message_id: int, text: str, buttons) -> bool:
        try:
            await self._client.edit_message(
                int(chat_id),
                int(message_id),
                text,
                buttons=buttons,
                parse_mode="md",
            )
            return True
        except Exception as exc:  # noqa: BLE001 - the page may be gone
            if type(exc).__name__ == "MessageNotModifiedError":
                return True
            return False



    @staticmethod



    @staticmethod
    def _summary_composition(summary) -> str:
        parts: list[str] = []
        if summary.video_count:
            parts.append(f"🎬{summary.video_count}")
        if summary.photo_count:
            parts.append(f"🖼{summary.photo_count}")
        if not parts:
            parts.append(f"📄{summary.item_count}")
        return " ".join(parts)

    @classmethod
    def _summary_label(cls, summary) -> str:
        if summary.item_count > 1:
            return f"相册 {summary.item_count} 项 · {cls._summary_composition(summary)}".strip()
        kinds = summary.kinds or ()
        return _KIND_WORDS.get(kinds[0], "媒体") if kinds else "媒体"

    @classmethod
    def _summary_line(cls, summary) -> str:
        return f"{cls._summary_label(summary)} · {cls._human_bytes(summary.size_bytes)}"

    # ----------------------------------------------------------- source page
    async def _source_page(self, owner_id: int) -> tuple[str, list]:
        coordinator = self._source_coordinator()
        lines = [
            "🔐 **来源账号（个人 session）**",
            "──────────",
            "用你自己的账号读取「机器人看不到的内容」，再发布到频道。",
            "不会自动监听：只处理你用 `/pick` 选择的内容。",
            "──────────",
        ]
        rows: list[list] = []
        if coordinator is None:
            lines.append("功能未装配，请联系维护者。")
            rows.append([Button.inline("🏠 首页", b"ui:home")])
            return "\n".join(lines), rows
        lines.append(f"状态：{coordinator.status_line()}")
        chats = coordinator.whitelist()
        if chats:
            lines.append(f"来源白名单（{len(chats)}）：")
            for entry in chats[:10]:
                lines.append(f"· `{entry}`")
        else:
            lines.append("来源白名单：`空`（未添加来源时无法抓取）")
        if coordinator.active:
            rows.append([Button.inline("📥 选择最近媒体", b"ui:pick:0:0")])
            rows.append([Button.inline("➕ 添加来源", b"ui:source-add")])
            rows.append([Button.inline("🗑 删除来源", b"ui:source-list")])
            rows.append([Button.inline("🚪 退出登录", b"ui:source-logout")])
        else:
            rows.append([Button.inline("📱 登录 / 重新登录", b"ui:source-login")])
            if chats:
                rows.append([Button.inline("🗑 删除来源", b"ui:source-list")])
        rows.append([Button.inline("🔄 刷新", b"ui:source"), Button.inline("🏠 首页", b"ui:home")])
        return "\n".join(lines), rows

    async def _source_list_page(self, owner_id: int) -> tuple[str, list]:
        coordinator = self._source_coordinator()
        chats = coordinator.whitelist() if coordinator is not None else []
        rows: list[list] = []
        if not chats:
            text = "🗑 **来源白名单**\n──────────\n当前为空。"
        else:
            text = "🗑 **来源白名单**\n──────────\n点下面的按钮移除对应来源。"
            for index, entry in enumerate(chats):
                rows.append([Button.inline(f"🗑 {entry[:32]}", f"ui:source-del:{index}".encode())])
        rows.append([Button.inline("⬅️ 返回", b"ui:source"), Button.inline("🏠 首页", b"ui:home")])
        return text, rows

    # ------------------------------------------------------- grab + confirm
    async def _confirm_pick_callback(
        self,
        event,
        owner_id: int,
        source_index: int,
        message_id: int,
        page: int,
    ) -> None:
        summary = self._pick_cache().get((int(owner_id), int(source_index), int(page)), {}).get(
            int(message_id)
        )
        lines = [
            "确认抓取",
            "──────────",
        ]
        lines.append(self._summary_line(summary) if summary is not None else "这条媒体")
        if summary is not None and summary.item_count > 1:
            lines.append(f"整组抓取 {summary.item_count} 项（来源相册不会被拆开）。")
        lines.append("──────────")
        rows = [
            [
                Button.inline(
                    "✅ 抓取",
                    f"ui:sy:{int(source_index)}:{int(message_id)}:{int(page)}".encode(),
                ),
                Button.inline(
                    "👁 先看",
                    f"ui:sv:{int(source_index)}:{int(message_id)}:{int(page)}".encode(),
                ),
            ],
            [
                Button.inline(
                    "❌ 取消",
                    f"ui:sn:{int(source_index)}:{int(page)}".encode(),
                )
            ],
        ]
        await self._edit_page(event, "\n".join(lines), rows)

    async def _confirmed_pick_callback(
        self,
        event,
        owner_id: int,
        source_index: int,
        message_id: int,
        page: int,
    ) -> None:
        coordinator = self._source_coordinator()
        if coordinator is None:
            await self._safe_answer(event, "来源功能不可用", alert=True)
            return
        if self._row_submitted(owner_id, source_index, message_id, page):
            await self._safe_answer(event, "这条已经提交过了，不会再抓取", alert=True)
            return
        await self._safe_answer(event, "已提交，正在抓取…")
        chat_id = int(event.chat_id)
        summary = self._pick_cache().get(
            (int(owner_id), int(source_index), int(page)), {}
        ).get(int(message_id))
        fingerprint = str(getattr(summary, "fingerprint", "") or "")
        try:
            count, label, accepted, skipped = await coordinator.grab_message(
                source_index, message_id, fingerprint=fingerprint
            )
        except Exception:  # noqa: BLE001 - user-facing miss
            await self._send_text(chat_id, "⚠️ 抓取失败，请稍后重试。")
            return
        if count:
            await self._clear_pick_previews(owner_id, chat_id)
            lines = [f"✅ 已抓取 `{accepted}` 个媒体（来源：`{label}`），正在下载与发布。"]
            if skipped:
                lines.append(f"跳过 `{skipped}` 项：之前已经发布过。")
            await self._send_text(chat_id, "\n".join(lines))
        else:
            await self._send_text(
                chat_id,
                f"⚠️ `#{message_id}` 已不可读取（可能被源删除）；已刷新列表。",
            )
        await self._show_pick_callback(
            event, owner_id, source_index, page, with_grid=False
        )

    # -------------------------------------------------------------- plumbing
    async def _send_text(self, chat_id: int, text: str):
        try:
            return await self._client.send_message(int(chat_id), text)
        except Exception:  # noqa: BLE001
            return None

    async def _edit_text(self, chat_id: int, message_id: int, text: str) -> None:
        with suppress(Exception):
            await self._client.edit_message(int(chat_id), int(message_id), text)

    async def _delete_message(self, chat_id: int, message_id: int) -> None:
        with suppress(Exception):
            await self._client.delete_messages(int(chat_id), [int(message_id)])

    async def _send_photo(self, chat_id: int, path, caption: str, buttons):
        try:
            return await self._client.send_file(
                int(chat_id),
                str(path),
                caption=caption,
                buttons=buttons,
                force_document=False,
            )
        except Exception:  # noqa: BLE001 - a failed photo must not break the page
            return None

    async def _pick_command(self, event, owner_id: int, argument: str) -> None:
        """``/pick [来源序号] [页码]``."""

        numbers = [int(token) for token in (argument or "").split() if token.isdigit()]
        source_index = max(0, numbers[0] - 1) if numbers else 0
        page = max(0, numbers[1] - 1) if len(numbers) > 1 else 0
        text, rows, summaries = await self._pick_render(owner_id, source_index, page)
        try:
            sent = await event.respond(text, buttons=rows, parse_mode="md")
        except Exception:  # noqa: BLE001
            sent = await self._send_text(int(event.chat_id), text)
        sent_id = getattr(sent, "id", None)
        if sent_id is not None:
            self._pick_list_messages()[int(owner_id)] = int(sent_id)
        self._start_page_grid(
            owner_id,
            int(event.chat_id),
            source_index,
            page,
            [summary.message_id for summary in summaries],
        )

    async def _handle_source_callback(self, event, owner_id: int, action: str) -> bool:
        """Dispatch source-setup and pick callbacks; returns True when handled."""

        if not action.startswith(_ACTIONS):
            return False
        coordinator = self._source_coordinator()
        if action == "ui:source":
            text, rows = await self._source_page(owner_id)
            await self._edit_page(event, text, rows)
            return True
        if action == "ui:source-list":
            text, rows = await self._source_list_page(owner_id)
            await self._edit_page(event, text, rows)
            return True
        if action.startswith("ui:pick:"):
            source_index, page = self._two_ints(action, 2, 3)
            await self._show_pick_callback(event, owner_id, source_index, page)
            return True
        if action.startswith("ui:pr:"):
            source_index, page = self._two_ints(action, 2, 3)
            await self._show_pick_callback(
                event, owner_id, source_index, max(0, page), refresh=True
            )
            return True
        if action.startswith("ui:sp:"):
            source_index, page = self._two_ints(action, 2, 3)
            await self._show_pick_callback(event, owner_id, source_index, max(0, page))
            return True
        if action.startswith("ui:sf:"):
            source_index, page = self._two_ints(action, 2, 3)
            current = self._pick_filter()
            current[int(owner_id)] = not bool(current.get(int(owner_id)))
            await self._show_pick_callback(event, owner_id, source_index, max(0, page))
            return True
        if action.startswith("ui:sd:"):
            parts = action.split(":")
            try:
                source_index = int(parts[2])
                page = int(parts[3])
                window = "2d" if parts[4] == "d" else "today"
            except (IndexError, ValueError):
                source_index, page, window = 0, 0, "today"
            self._pick_window()[int(owner_id)] = window
            await self._show_pick_callback(event, owner_id, source_index, max(0, page))
            return True
        if action.startswith("ui:sk:"):
            parts = action.split(":")
            try:
                source_index = int(parts[2])
                page = int(parts[3])
                message_id = int(parts[4])
            except (IndexError, ValueError):
                await self._safe_answer(event, "操作已过期", alert=True)
                return True
            if self._row_submitted(owner_id, source_index, message_id, page):
                await self._safe_answer(event, "这条已经提交过了，不会再抓取", alert=True)
                return True
            summary = self._pick_cache().get(
                (int(owner_id), int(source_index), int(page)), {}
            ).get(int(message_id))
            if summary is None:
                await self._safe_answer(event, "这一页已刷新，请重新选择", alert=True)
                return True
            self._toggle_selection(owner_id, source_index, summary)
            await self._show_pick_callback(
                event, owner_id, source_index, page, with_grid=False
            )
            return True
        if action.startswith("ui:pk:"):
            parts = action.split(":")
            if len(parts) < 5:
                await self._safe_answer(event, "操作已过期", alert=True)
                return True
            try:
                source_index = int(parts[2])
                message_id = int(parts[3])
                page = int(parts[4])
            except ValueError:
                await self._safe_answer(event, "操作已过期", alert=True)
                return True
            await self._toggle_preview_selection(
                event, owner_id, source_index, message_id, page
            )
            return True
        if action.startswith("ui:srm:"):
            parts = action.split(":")
            try:
                source_index = int(parts[2])
                page = int(parts[3])
                message_id = int(parts[4])
            except (IndexError, ValueError):
                await self._safe_answer(event, "操作已过期", alert=True)
                return True
            await self._remove_from_selection(
                event, owner_id, source_index, message_id, page
            )
            return True
        if action.startswith("ui:pdr:"):
            parts = action.split(":")
            try:
                source_index = int(parts[2])
                message_id = int(parts[3])
                page = int(parts[4]) if len(parts) > 4 else 0
            except (IndexError, ValueError):
                await self._safe_answer(event, "操作已过期", alert=True)
                return True
            await self._release_done_callback(
                event, owner_id, source_index, message_id, page
            )
            return True
        if action.startswith("ui:ps:"):
            source_index, page = self._two_ints(action, 2, 3)
            await self._show_submitted_callback(
                event, owner_id, source_index, max(0, page)
            )
            return True
        if action.startswith("ui:sx:"):
            source_index, page = self._two_ints(action, 2, 3)
            self._pick_selection().pop(int(owner_id), None)
            await self._safe_answer(event, "已清空选择")
            await self._show_pick_callback(
                event, owner_id, source_index, max(0, page), with_grid=False
            )
            return True
        if action.startswith("ui:sz:"):
            source_index, page = self._two_ints(action, 2, 3)
            await self._merge_confirm_card(event, owner_id, source_index, page)
            return True
        if action.startswith("ui:sm:"):
            source_index, page = self._two_ints(action, 2, 3)
            await self._publish_merged(event, owner_id, source_index, page)
            return True
        if action.startswith("ui:spm:"):
            source_index, page = self._two_ints(action, 2, 3)
            await self._merged_preview(event, owner_id, source_index, page)
            return True
        if action.startswith("ui:sa:"):
            source_index, page = self._two_ints(action, 2, 3)
            await self._toggle_ad_filter(event, owner_id, source_index, page)
            return True
        if action.startswith("ui:sh:"):
            source_index, page = self._two_ints(action, 2, 3)
            await self._show_hidden_callback(event, owner_id, source_index, max(0, page))
            return True
        if action.startswith("ui:sr:"):
            parts = action.split(":")
            try:
                source_index = int(parts[2])
                message_id = int(parts[3])
                page = int(parts[4]) if len(parts) > 4 else 0
            except (IndexError, ValueError):
                await self._safe_answer(event, "操作已过期", alert=True)
                return True
            await self._release_ad_callback(
                event, owner_id, source_index, message_id, page
            )
            return True
        if action.startswith("ui:sn:"):
            source_index, page = self._two_ints(action, 2, 3)
            await self._show_pick_callback(
                event, owner_id, source_index, max(0, page), with_grid=False
            )
            return True
        if action.startswith("ui:sv:"):
            parts = action.split(":")
            if len(parts) < 4:
                await self._safe_answer(event, "操作已过期", alert=True)
                return True
            try:
                source_index = int(parts[2])
                message_id = int(parts[3])
                page = int(parts[4]) if len(parts) > 4 else 0
                position = int(parts[5]) if len(parts) > 5 else 0
            except ValueError:
                await self._safe_answer(event, "操作已过期", alert=True)
                return True
            await self._preview_single(
                event, owner_id, source_index, message_id, page, position
            )
            return True
        if action.startswith("ui:pc:"):
            source_index, page = self._two_ints(action, 2, 3)
            removed = await self._clear_pick_previews(owner_id, int(event.chat_id))
            await self._safe_answer(
                event, f"已清理 {removed} 张预览" if removed else "没有待清理的预览"
            )
            await self._show_pick_callback(
                event, owner_id, source_index, max(0, page), with_grid=False
            )
            return True
        if action.startswith("ui:sg:"):
            parts = action.split(":")
            if len(parts) < 4:
                await self._safe_answer(event, "操作已过期", alert=True)
                return True
            try:
                source_index = int(parts[2])
                message_id = int(parts[3])
                page = int(parts[4]) if len(parts) > 4 else 0
            except ValueError:
                await self._safe_answer(event, "操作已过期", alert=True)
                return True
            await self._confirm_pick_callback(
                event, owner_id, source_index, message_id, page
            )
            return True
        if action.startswith("ui:sy:"):
            parts = action.split(":")
            if len(parts) < 4:
                await self._safe_answer(event, "操作已过期", alert=True)
                return True
            try:
                source_index = int(parts[2])
                message_id = int(parts[3])
                page = int(parts[4]) if len(parts) > 4 else 0
            except ValueError:
                await self._safe_answer(event, "操作已过期", alert=True)
                return True
            await self._clear_pick_previews(owner_id, int(event.chat_id))
            await self._confirmed_pick_callback(
                event, owner_id, source_index, message_id, page
            )
            return True
        if coordinator is None:
            await self._safe_answer(event, "来源功能不可用", alert=True)
            return True
        if action == "ui:source-login":
            coordinator.set_awaiting("phone")
            await self._safe_answer(event, "请发送手机号")
            await self._send_text(
                int(event.chat_id),
                "📱 **登录来源账号**\n"
                "──────────\n"
                "请把该账号的**手机号**发给我（含国家码，例如 `+8613800138000`）。\n"
                "验证码会发到你账号的 Telegram 或短信；我不会记录、也不会外泄。",
            )
            return True
        if action == "ui:source-add":
            coordinator.set_awaiting("add_chat")
            await self._safe_answer(event, "请发送来源")
            await self._send_text(
                int(event.chat_id),
                "➕ **添加来源**\n"
                "──────────\n"
                "发送 `@频道名` 或 `-100` 开头的数字 ID；该账号需要已经加入这个聊天。",
            )
            return True
        if action == "ui:source-logout":
            try:
                message = await coordinator.logout()
            except SourceLoginError as exc:
                await self._safe_answer(event, str(exc), alert=True)
                return True
            await self._safe_answer(event, "已退出")
            await self._send_text(int(event.chat_id), f"🚪 {message}")
            text, rows = await self._source_page(owner_id)
            await self._edit_page(event, text, rows)
            return True
        if action.startswith("ui:source-del:"):
            try:
                index = int(action.rsplit(":", 1)[1])
            except (TypeError, ValueError):
                await self._safe_answer(event, "操作已过期", alert=True)
                return True
            try:
                message = await coordinator.remove_chat(index)
            except SourceLoginError as exc:
                await self._safe_answer(event, str(exc), alert=True)
                return True
            await self._safe_answer(event, "已移除")
            text, rows = await self._source_list_page(owner_id)
            await self._edit_page(event, f"{message}\n\n{text}", rows)
            return True
        return False

    @staticmethod
    def _two_ints(action: str, first: int, second: int) -> tuple[int, int]:
        parts = action.split(":")
        try:
            return (int(parts[first]), int(parts[second]))
        except (IndexError, ValueError):
            return (0, 0)
