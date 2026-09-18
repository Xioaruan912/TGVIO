from __future__ import annotations

from telethon import Button

from tgvio.adapters.telegram.bot_ui_support import *  # noqa: F401,F403
from tgvio.adapters.telegram.source_runtime import SourceCoordinator, SourceLoginError


class BotUISourceMixin:
    """In-Bot personal-account source setup: login, logout and whitelist."""

    def _source_coordinator(self) -> SourceCoordinator | None:
        return getattr(self, "_source", None)

    async def _source_page(self, owner_id: int) -> tuple[str, list]:
        coordinator = self._source_coordinator()
        lines = [
            "🔐 **来源账号（个人 session）**",
            "──────────",
            "用于抓取 bot 看不到的内容（私聊机器人、受限频道）。",
            "开启后**不会自动监听**：只有你主动触发才会抓取。",
            "──────────",
        ]
        rows: list[list] = []
        if coordinator is None:
            lines.append("功能未装配，请联系维护者。")
            rows.append([Button.inline("🏠 首页", b"ui:home")])
            return "\n".join(lines), rows
        lines.append(f"状态：{coordinator.status_line()}")
        trigger = coordinator.effective_trigger()
        chats = coordinator.whitelist()
        if chats:
            lines.append(f"来源白名单（{len(chats)}）：")
            for entry in chats[:10]:
                lines.append(f"· `{entry}`")
        else:
            lines.append("来源白名单：`空`（未配置时不会抓取任何内容）")
        lines.append(f"触发词：`{trigger}`（回复目标消息后发送它）")
        if coordinator.active:
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

    async def _handle_source_callback(self, event, owner_id: int, action: str) -> bool:
        """Dispatch source-setup callbacks; returns True when handled."""

        if not action.startswith("ui:source"):
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

    async def _send_page(self, chat_id: int, text: str) -> None:
        try:
            await self._client.send_message(chat_id, text)
        except Exception:
            pass
