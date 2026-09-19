from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telethon import Button

from tgvio.adapters.telegram.bot_ui_support import *  # noqa: F401,F403
from tgvio.adapters.telegram.source_runtime import SourceCoordinator, SourceLoginError
from tgvio.domain.job import MediaKind

_LOCAL_TZ = ZoneInfo("Asia/Shanghai")
_KIND_LABELS: dict[MediaKind, str] = {
    MediaKind.PHOTO: "🖼",
    MediaKind.VIDEO: "🎬",
    MediaKind.AUDIO: "🎵",
    MediaKind.DOCUMENT: "📄",
}
_KIND_WORDS: dict[MediaKind, str] = {
    MediaKind.PHOTO: "🖼 图片",
    MediaKind.VIDEO: "🎬 视频",
    MediaKind.AUDIO: "🎵 音频",
    MediaKind.DOCUMENT: "📄 文件",
}
_PICK_PAGE_SIZE = 10
_PREVIEW_TTL_SECONDS = 60

_ACTIONS = ("ui:source", "ui:pick", "ui:sp:", "ui:sg", "ui:sy", "ui:sn", "ui:sv", "ui:sf")


class BotUISourceMixin:
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

    def _pick_grid_messages(self) -> dict[int, int]:
        state = getattr(self, "_pick_grid_msgs", None)
        if state is None:
            state = {}
            self._pick_grid_msgs = state
        return state

    def _pick_preview_messages(self) -> dict[int, int]:
        state = getattr(self, "_pick_preview_msgs", None)
        if state is None:
            state = {}
            self._pick_preview_msgs = state
        return state

    @staticmethod
    def _summary_composition(summary) -> str:
        counts: dict[MediaKind, int] = {}
        for kind in summary.kinds:
            counts[kind] = counts.get(kind, 0) + 1
        return " ".join(
            f"{_KIND_LABELS.get(kind, '')}{count}" for kind, count in counts.items()
        )

    @classmethod
    def _summary_label(cls, summary) -> str:
        if summary.item_count > 1:
            composition = cls._summary_composition(summary)
            return f"🧩 相册 {summary.item_count} 项 · {composition}".strip()
        kinds = summary.kinds or ()
        return _KIND_WORDS.get(kinds[0], "媒体") if kinds else "媒体"

    @classmethod
    def _summary_line(cls, summary) -> str:
        return (
            f"{cls._summary_label(summary)} · {cls._human_bytes(summary.size_bytes)}"
            f" · {cls._summary_time(summary)} · #{summary.message_id}"
        )

    @staticmethod
    def _summary_time(summary) -> str:
        date = summary.date
        if date is None:
            return "--:--"
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.astimezone(_LOCAL_TZ).strftime("%m-%d %H:%M")

    # ----------------------------------------------------------- source page
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

    # ------------------------------------------------------------- pick page
    async def _pick_render(
        self,
        owner_id: int,
        source_index: int = 0,
        page: int = 0,
    ) -> tuple[str, list, list]:
        """Render one pick page: text, buttons and the visible summaries."""

        coordinator = self._source_coordinator()
        if coordinator is None:
            return ("来源功能未装配。", [[Button.inline("🏠 首页", b"ui:home")]], [])
        summaries, has_more, label = await coordinator.list_media(
            source_index, page=page, page_size=_PICK_PAGE_SIZE
        )
        summaries = list(summaries)
        video_only = bool(self._pick_filter().get(int(owner_id)))
        if video_only:
            summaries = [summary for summary in summaries if summary.has_video]
        self._pick_cache()[(int(owner_id), int(source_index), int(page))] = {
            summary.message_id: summary for summary in summaries
        }
        header = f"📥 **选择要发布的内容**｜来源：`{label or '未配置'}`｜第 {page + 1} 页"
        if video_only:
            header += "｜🎬 只看视频"
        lines = [header, "──────────"]
        rows: list[list] = []
        if not summaries:
            lines.append("这一页没有符合条件的媒体（可以翻页或关掉筛选）。")
        for position, summary in enumerate(summaries, start=1):
            lines.append(f"{position}) {self._summary_line(summary)}")
            rows.append(
                [
                    Button.inline(
                        f"📥 {position}",
                        f"ui:sg:{int(source_index)}:{summary.message_id}:{int(page)}".encode(),
                    ),
                    Button.inline(
                        f"👁 {position}",
                        f"ui:sv:{int(source_index)}:{summary.message_id}:{int(page)}".encode(),
                    ),
                ]
            )
        lines.append("──────────")
        lines.append("📥 = 抓取（先确认）· 👁 = 只看缩略图")
        nav: list = []
        if page > 0:
            nav.append(Button.inline("⬅️ 上一页", f"ui:sp:{int(source_index)}:{page - 1}".encode()))
        if has_more:
            nav.append(Button.inline("下一页 ➡️", f"ui:sp:{int(source_index)}:{page + 1}".encode()))
        if nav:
            rows.append(nav)
        filter_label = "🖼 全部" if video_only else "🎬 只看视频"
        rows.append(
            [
                Button.inline(
                    filter_label,
                    f"ui:sf:{int(source_index)}:{int(page)}".encode(),
                ),
                Button.inline("🔄 刷新", f"ui:pick:{int(source_index)}:{int(page)}".encode()),
            ]
        )
        rows.append([Button.inline("⬅️ 来源设置", b"ui:source"), Button.inline("🏠 首页", b"ui:home")])
        return "\n".join(lines), rows, summaries

    async def _show_pick_callback(
        self,
        event,
        owner_id: int,
        source_index: int,
        page: int,
        *,
        with_grid: bool = True,
    ) -> None:
        text, rows, summaries = await self._pick_render(owner_id, source_index, page)
        await self._edit_page(event, text, rows)
        if with_grid:
            self._start_page_grid(
                owner_id,
                int(event.chat_id),
                source_index,
                page,
                [summary.message_id for summary in summaries],
            )

    # ------------------------------------------------------------ grid build
    def _start_page_grid(
        self,
        owner_id: int,
        chat_id: int,
        source_index: int,
        page: int,
        message_ids: list[int],
    ) -> None:
        service = self._pick_preview_service()
        if service is None or not message_ids:
            return
        task = asyncio.ensure_future(
            self._build_page_grid(owner_id, chat_id, source_index, page, list(message_ids))
        )
        tasks = getattr(self, "_tasks", None)
        if tasks is not None:
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    async def _build_page_grid(
        self,
        owner_id: int,
        chat_id: int,
        source_index: int,
        page: int,
        message_ids: list[int],
    ) -> None:
        service = self._pick_preview_service()
        if service is None:
            return
        total = len(message_ids)
        progress = await self._send_text(chat_id, f"⏳ 正在生成第 {page + 1} 页缩略图 0/{total} …")
        progress_id = getattr(progress, "id", None)
        last = 0

        async def report(done: int, count: int) -> None:
            nonlocal last
            if progress_id is None or done == last:
                return
            last = done
            await self._edit_text(
                chat_id,
                int(progress_id),
                f"⏳ 正在生成第 {page + 1} 页缩略图 {done}/{count} …",
            )

        try:
            preview = await service.build_page(source_index, message_ids, progress=report)
        except Exception:  # noqa: BLE001 - previews must never break picking
            preview = None
        if preview is None or preview.image is None:
            available = 0 if preview is None else preview.fetched
            if progress_id is not None:
                await self._edit_text(
                    chat_id,
                    int(progress_id),
                    f"⚠️ 缩略图不可用（{available}/{total}）；可点每行 👁 单独看。",
                )
            return
        previous = self._pick_grid_messages().pop(int(owner_id), None)
        if previous is not None:
            await self._delete_message(chat_id, previous)
        if progress_id is not None:
            await self._delete_message(chat_id, int(progress_id))
        columns, rows = getattr(service, "grid_shape", lambda count: (5, 2))(total)
        caption = (
            f"🖼 第 {page + 1} 页缩略图 · {columns}×{rows}\n"
            "位置从左到右、从上到下依次对应上面列表的 1)…N)。"
        )
        message = await self._send_photo(
            chat_id,
            preview.image,
            caption,
            [
                [
                    Button.inline(
                        "🔄 整页刷新",
                        f"ui:pick:{int(source_index)}:{int(page)}".encode(),
                    ),
                    Button.inline("⬅️ 返回列表", b"ui:source"),
                ]
            ],
        )
        service.release(preview.token)
        if message is not None and getattr(message, "id", None) is not None:
            self._pick_grid_messages()[int(owner_id)] = int(message.id)

    # --------------------------------------------------------- row previews
    async def _preview_single(
        self,
        event,
        owner_id: int,
        source_index: int,
        message_id: int,
        page: int,
    ) -> None:
        service = self._pick_preview_service()
        chat_id = int(event.chat_id)
        if service is None:
            await self._safe_answer(event, "预览不可用", alert=True)
            return
        await self._safe_answer(event, "正在获取预览…")
        summary = self._pick_cache().get((int(owner_id), int(source_index), int(page)), {}).get(
            int(message_id)
        )
        progress = await self._send_text(chat_id, f"⏳ 正在获取 `#{message_id}` 的缩略图 …")
        progress_id = getattr(progress, "id", None)
        try:
            preview = await service.build_single(source_index, int(message_id))
        except Exception:  # noqa: BLE001
            preview = None
        if preview is None or preview.image is None:
            if progress_id is not None:
                await self._edit_text(
                    chat_id,
                    int(progress_id),
                    f"⚠️ `#{message_id}` 没有可用缩略图；可直接点 📥 抓取。",
                )
            return
        if progress_id is not None:
            await self._delete_message(chat_id, int(progress_id))
        previous = self._pick_preview_messages().pop(int(owner_id), None)
        if previous is not None:
            await self._delete_message(chat_id, previous)
        caption = f"👁 `#{message_id}`"
        if summary is not None:
            caption += f"\n{self._summary_line(summary)}"
        message = await self._send_photo(
            chat_id,
            preview.image,
            caption,
            [
                [
                    Button.inline(
                        "📥 抓取这条",
                        f"ui:sg:{int(source_index)}:{int(message_id)}:{int(page)}".encode(),
                    ),
                    Button.inline(
                        "⬅️ 返回列表",
                        f"ui:pick:{int(source_index)}:{int(page)}".encode(),
                    ),
                ]
            ],
        )
        service.release(preview.token)
        if message is not None and getattr(message, "id", None) is not None:
            message_id_value = int(message.id)
            self._pick_preview_messages()[int(owner_id)] = message_id_value
            self._schedule_preview_expiry(owner_id, chat_id, message_id_value)

    def _schedule_preview_expiry(self, owner_id: int, chat_id: int, message_id: int) -> None:
        task = asyncio.ensure_future(
            self._expire_preview(owner_id, chat_id, message_id)
        )
        tasks = getattr(self, "_tasks", None)
        if tasks is not None:
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    async def _expire_preview(self, owner_id: int, chat_id: int, message_id: int) -> None:
        try:
            await asyncio.sleep(_PREVIEW_TTL_SECONDS)
        except asyncio.CancelledError:
            return
        if self._pick_preview_messages().get(int(owner_id)) != int(message_id):
            return
        self._pick_preview_messages().pop(int(owner_id), None)
        await self._delete_message(chat_id, int(message_id))

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
            f"📥 **确认抓取** `#{message_id}`",
            "──────────",
        ]
        lines.append(self._summary_line(summary) if summary is not None else f"`#{message_id}`")
        if summary is not None and summary.item_count > 1:
            lines.append(f"这将整组抓取 `{summary.item_count}` 项（来源相册不会被拆开）。")
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
        await self._safe_answer(event, "已提交，正在抓取…")
        chat_id = int(event.chat_id)
        try:
            count, label = await coordinator.grab_message(source_index, message_id)
        except Exception:  # noqa: BLE001 - user-facing miss
            await self._send_text(chat_id, "⚠️ 抓取失败，请稍后重试。")
            return
        if count:
            await self._send_text(
                chat_id,
                f"✅ 已抓取 `{count}` 个媒体（来源：`{label}`），正在下载与发布。",
            )
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
            await event.respond(text, buttons=rows, parse_mode="md")
        except Exception:  # noqa: BLE001
            await self._send_text(int(event.chat_id), text)
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
            except ValueError:
                await self._safe_answer(event, "操作已过期", alert=True)
                return True
            await self._preview_single(event, owner_id, source_index, message_id, page)
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
