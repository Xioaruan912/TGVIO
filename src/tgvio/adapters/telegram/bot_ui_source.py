from __future__ import annotations

from datetime import timezone
from zoneinfo import ZoneInfo

from telethon import Button

from tgvio.adapters.telegram.bot_ui_support import *  # noqa: F401,F403
from tgvio.adapters.telegram.source_runtime import SourceCoordinator, SourceLoginError
from tgvio.domain.job import MediaKind

_LOCAL_TZ = ZoneInfo("Asia/Shanghai")
_KIND_LABELS: dict[MediaKind, str] = {
    MediaKind.PHOTO: "🖼 图片",
    MediaKind.VIDEO: "🎬 视频",
    MediaKind.AUDIO: "🎵 音频",
    MediaKind.DOCUMENT: "📄 文件",
}
_PICK_PAGE_SIZE = 10


class BotUISourceMixin:
    """In-Bot personal-account source setup and content picking."""

    def _source_coordinator(self) -> SourceCoordinator | None:
        return getattr(self, "_source", None)

    async def _source_page(self, owner_id: int) -> tuple[str, list]:
        coordinator = self._source_coordinator()
        lines = [
            "🔐 **来源账号（个人 session）**",
            "──────────",
            "用你自己的账号读取「机器人看不到的内容」，再发布到频道。",
            "不会自动监听：只处理你用 `/pick` 或 `/grab` 选择的内容。",
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

    async def _pick_page(
        self,
        owner_id: int,
        source_index: int = 0,
        page: int = 0,
    ) -> tuple[str, list]:
        coordinator = self._source_coordinator()
        if coordinator is None:
            return ("来源功能未装配。", [[Button.inline("🏠 首页", b"ui:home")]])
        summaries, has_more, label = await coordinator.list_media(
            source_index, page=page, page_size=_PICK_PAGE_SIZE
        )
        lines = [
            f"📥 **选择要发布的内容**｜来源：`{label or '未配置'}`｜第 {page + 1} 页",
            "──────────",
        ]
        rows: list[list] = []
        if not summaries:
            lines.append("这一页没有媒体（可以试试返回上一页）。")
        for position, summary in enumerate(summaries, start=1):
            lines.append(
                f"{position}) {self._summary_label(summary)} · "
                f"{self._human_bytes(summary.size_bytes)} · {self._summary_time(summary)}"
            )
            rows.append(
                [
                    Button.inline(
                        f"📥 {position}",
                        f"ui:sg:{int(source_index)}:{summary.message_id}".encode("utf-8"),
                    )
                ]
            )
        lines.append("──────────")
        lines.append("点 📥 抓取该条；相册会整组抓取。")
        nav: list = []
        if page > 0:
            nav.append(Button.inline("⬅️ 上一页", f"ui:sp:{int(source_index)}:{page - 1}".encode()))
        if has_more:
            nav.append(Button.inline("下一页 ➡️", f"ui:sp:{int(source_index)}:{page + 1}".encode()))
        if nav:
            rows.append(nav)
        rows.append(
            [
                Button.inline("🔄 刷新", f"ui:pick:{int(source_index)}:{page}".encode()),
                Button.inline("⬅️ 来源设置", b"ui:source"),
            ]
        )
        rows.append([Button.inline("🏠 首页", b"ui:home")])
        return "\n".join(lines), rows

    async def _show_pick_callback(
        self,
        event,
        owner_id: int,
        source_index: int,
        page: int,
    ) -> None:
        text, rows = await self._pick_page(owner_id, source_index, page)
        await self._edit_page(event, text, rows)

    async def _grab_pick_callback(
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
        await self._safe_answer(event, "正在抓取…")
        try:
            count, label = await coordinator.grab_message(source_index, message_id)
        except Exception:
            await self._safe_send(int(event.chat_id), "⚠️ 抓取失败，请稍后重试。")
            return
        if count:
            await self._send_page(
                int(event.chat_id),
                f"✅ 已抓取 {count} 个媒体（来源：{label}），正在下载与发布。",
            )
        else:
            await self._send_page(
                int(event.chat_id),
                "⚠️ 这条消息已不可读取（可能被机器人删除）；请刷新后再选。",
            )
        await self._show_pick_callback(event, owner_id, source_index, page)

    async def _pick_command(self, event, owner_id: int, argument: str) -> None:
        """``/pick [来源序号] [页码]``."""

        numbers = [int(token) for token in (argument or "").split() if token.isdigit()]
        source_index = max(0, numbers[0] - 1) if numbers else 0
        page = max(0, numbers[1] - 1) if len(numbers) > 1 else 0
        text, rows = await self._pick_page(owner_id, source_index, page)
        try:
            await event.respond(text, buttons=rows, parse_mode="md")
        except Exception:
            await self._send_page(int(event.chat_id), text)

    async def _handle_source_callback(self, event, owner_id: int, action: str) -> bool:
        """Dispatch source-setup callbacks; returns True when handled."""

        if not action.startswith("ui:source") and not action.startswith("ui:pick") and not action.startswith("ui:sg:") and not action.startswith("ui:sp:"):
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
            parts = action.split(":")
            try:
                source_index = int(parts[2]) if len(parts) > 2 else 0
                page = int(parts[3]) if len(parts) > 3 else 0
            except (TypeError, ValueError):
                source_index, page = 0, 0
            await self._show_pick_callback(event, owner_id, source_index, page)
            return True
        if action.startswith("ui:sp:"):
            parts = action.split(":")
            try:
                source_index = int(parts[2])
                page = max(0, int(parts[3]))
            except (IndexError, TypeError, ValueError):
                await self._safe_answer(event, "操作已过期", alert=True)
                return True
            await self._show_pick_callback(event, owner_id, source_index, page)
            return True
        if action.startswith("ui:sg:"):
            parts = action.split(":")
            try:
                source_index = int(parts[2])
                message_id = int(parts[3])
                page = int(parts[4]) if len(parts) > 4 else 0
            except (IndexError, TypeError, ValueError):
                await self._safe_answer(event, "操作已过期", alert=True)
                return True
            await self._grab_pick_callback(event, owner_id, source_index, message_id, page)
            return True
        if coordinator is None:
            await self._safe_answer(event, "来源功能不可用", alert=True)
            return True
        if action == "ui:source-login":
            coordinator.set_awaiting("phone")
            await self._safe_answer(event, "请发送手机号")
            await self._send_page(
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
            await self._send_page(
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
            await self._send_page(int(event.chat_id), f"🚪 {message}")
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

    @classmethod
    def _summary_label(cls, summary) -> str:
        if summary.item_count > 1:
            labels = "".join(_KIND_LABELS.get(kind, "") for kind in summary.kinds)
            return f"🧩 相册 {summary.item_count} 项 {labels}".strip()
        kinds = summary.kinds or ()
        return _KIND_LABELS.get(kinds[0], "媒体") if kinds else "媒体"

    @staticmethod
    def _summary_time(summary) -> str:
        date = summary.date
        if date is None:
            return "--:--"
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.astimezone(_LOCAL_TZ).strftime("%m-%d %H:%M")

    async def _send_page(self, chat_id: int, text: str) -> None:
        try:
            await self._client.send_message(chat_id, text)
        except Exception:
            pass
