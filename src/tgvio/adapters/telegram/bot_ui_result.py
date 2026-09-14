from __future__ import annotations

from telethon import Button

from tgvio.adapters.telegram.bot_ui_support import *  # noqa: F401,F403
from tgvio.application.result_card import ResultCard, ResultCardService
from tgvio.domain.job import Job, JobState


_CARD_PAGE_SIZE = 5


class BotUIResultMixin:
    async def _quiet_enabled(self, owner_id: int) -> bool:
        try:
            preference = await self._repository.get_user_preference(int(owner_id))
            return bool(preference.quiet_mode)
        except Exception:
            return False

    def _result_service(self) -> ResultCardService:
        return ResultCardService(self._repository)

    async def _build_result(self, owner_id: int, job: Job) -> ResultCard:
        favorited = await self._repository.is_favorite(int(owner_id), job.id)
        undo_remaining = 0
        if self._undo_service is not None and job.terminal:
            try:
                status = await self._undo_service.status(job)
                undo_remaining = int(status.remaining_messages)
            except Exception:
                undo_remaining = 0
        return await self._result_service().build(
            job, favorited=favorited, undo_remaining=undo_remaining
        )

    async def _render_result_card(self, owner_id: int, job: Job) -> tuple[str, list]:
        card = await self._build_result(owner_id, job)
        size = sum(int(item.size_bytes) for item in job.items)
        labels = {
            "revoked": "↩️ 已撤销发布",
            "partially_revoked": "⚠️ 已部分撤销，仍有消息需处理",
            "succeeded": "✅ Telegram 发布完成",
            "uncertain": "🛡️ 需人工核对（可能已有可见消息）",
            "failed": "❌ 发布失败",
            "cancelled": "⛔ 已取消",
            "pending": "⏳ 处理中",
        }
        archive_labels = {
            None: "未启用归档",
            "committed": "✅ 已归档",
            "failed": "❌ 归档失败（可重传）",
            "cancelled": "⛔ 归档已取消",
            "uploading": "☁️ 归档上传中",
            "verifying": "☁️ 归档校验中",
            "staging": "☁️ 归档准备中",
            "planned": "☁️ 归档排队中",
        }
        lines = [
            f"📋 **任务结果** {await self._job_label(job)}",
            "──────────",
            labels.get(card.telegram_state, card.telegram_state),
            f"🎬 媒体：`{card.media_total}` 个 · `{self._human_bytes(size)}`",
            f"📢 已确认消息：`{card.confirmed_messages}` 条",
            f"☁️ WebDAV：{archive_labels.get(card.archive_state, card.archive_state or '未知')}",
        ]
        if card.link_url is None and card.link_reason:
            lines.append(f"🔗 {card.link_reason}")
        lines.append("──────────")
        star = "★ 已收藏" if card.favorited else "⭐ 收藏"
        rows: list[list] = []
        if card.link_url:
            rows.append([Button.url("🔗 打开帖子", card.link_url)])
        else:
            rows.append([Button.inline("ℹ️ 链接说明", self._callback_data("link-info", job.id))])
        rows.append(
            [
                Button.inline(star, self._callback_data("fav", job.id)),
                Button.inline("📤 分享", self._callback_data("share", job.id)),
            ]
        )
        rows.append(
            [
                Button.inline("🔁 再发一组", self._callback_data("repost", job.id)),
                Button.inline("🎨 同款再发", self._callback_data("restyle", job.id)),
            ]
        )
        if card.undo_remaining > 0:
            rows.append([Button.inline("↩️ 撤销发布", self._callback_data("undo", job.id))])
        rows.append(
            [
                Button.inline("🔎 任务详情", self._callback_data("job", job.id)),
                Button.inline("🗂 收藏夹", b"ui:favorites:0"),
            ]
        )
        rows.append([Button.inline("🏠 首页", b"ui:home")])
        return "\n".join(lines), rows

    async def _show_result_callback(self, event, owner_id: int, job_id: str) -> None:
        job = await self._repository.get(job_id)
        if job is None or int(job.owner_id) != int(owner_id):
            await self._safe_answer(event, "没有找到对应任务", alert=True)
            return
        text, rows = await self._render_result_card(owner_id, job)
        await self._edit_page(event, text, rows)

    async def _toggle_favorite_callback(self, event, owner_id: int, job_id: str) -> None:
        job = await self._repository.get(job_id)
        if job is None or int(job.owner_id) != int(owner_id):
            await self._safe_answer(event, "没有找到对应任务", alert=True)
            return
        if await self._repository.is_favorite(owner_id, job_id):
            await self._repository.remove_favorite(owner_id, job_id)
            await self._safe_answer(event, "已取消收藏")
        else:
            await self._repository.add_favorite(owner_id, job_id)
            await self._safe_answer(event, "已收藏")
        text, rows = await self._render_result_card(owner_id, job)
        await self._edit_page(event, text, rows)

    async def _link_info_callback(self, event, owner_id: int, job_id: str) -> None:
        job = await self._repository.get(job_id)
        if job is None or int(job.owner_id) != int(owner_id):
            await self._safe_answer(event, "没有找到对应任务", alert=True)
            return
        card = await self._build_result(owner_id, job)
        await self._safe_answer(event, card.link_reason or "无法生成链接", alert=True)

    async def _share_callback(self, event, owner_id: int, job_id: str) -> None:
        job = await self._repository.get(job_id)
        if job is None or int(job.owner_id) != int(owner_id):
            await self._safe_answer(event, "没有找到对应任务", alert=True)
            return
        card = await self._build_result(owner_id, job)
        if not card.link_url:
            await self._safe_answer(event, card.link_reason or "暂无可分享链接", alert=True)
            return
        share_url = f"https://t.me/share/url?url={card.link_url}"
        try:
            await self._client.send_message(
                event.chat_id,
                f"📤 分享链接（不会自动发送给他人）：\n{card.link_url}\n{share_url}",
            )
        except Exception:
            pass
        await self._safe_answer(event, "链接已发送到当前对话")

    async def _repost_callback(self, event, owner_id: int, job_id: str) -> None:
        await self._start_empty_collection(event, owner_id, style=None)

    async def _restyle_callback(self, event, owner_id: int, job_id: str) -> None:
        job = await self._repository.get(job_id)
        style = None
        if job is not None and int(job.owner_id) == int(owner_id):
            candidate = job.policy.get("publish_style")
            if isinstance(candidate, dict):
                style = candidate
        await self._start_empty_collection(event, owner_id, style=style)

    async def _start_empty_collection(
        self,
        event,
        owner_id: int,
        *,
        style: dict | None,
    ) -> None:
        intake = getattr(self, "_intake", None)
        if intake is None or not hasattr(intake, "begin_collection"):
            await self._safe_answer(event, "合集功能当前不可用", alert=True)
            return
        existing = await intake.open_collection(owner_id=int(owner_id), chat_id=int(event.chat_id))
        if existing is not None:
            await self._safe_answer(event, "已有收集中的合集，请先保存草稿或结束当前合集。", alert=True)
            return
        if style is not None:
            try:
                import json

                await self._repository.set_user_style(
                    int(owner_id), json.dumps(style, ensure_ascii=False)
                )
            except Exception:
                pass
        await intake.begin_collection(owner_id=int(owner_id), chat_id=int(event.chat_id))
        await self._safe_answer(event, "已开始新的空合集")
        try:
            await self._client.send_message(
                event.chat_id,
                "📥 **已开始新的空合集**\n\n发送图片/视频或文字，然后点“结束并发布”。",
                buttons=self._reply_keyboard(),
                parse_mode="md",
            )
        except Exception:
            pass

    async def _favorites_page_callback(self, event, owner_id: int, page_token: str) -> None:
        try:
            page = max(0, int(page_token))
        except (TypeError, ValueError):
            page = 0
        text, rows = await self._render_favorites(owner_id, page)
        await self._edit_page(event, text, rows)

    async def _render_favorites(self, owner_id: int, page: int) -> tuple[str, list]:
        total = await self._repository.count_favorites(owner_id)
        total_pages = max(1, (total + _CARD_PAGE_SIZE - 1) // _CARD_PAGE_SIZE)
        page = max(0, min(int(page), total_pages - 1))
        jobs = await self._repository.list_favorite_jobs(
            owner_id,
            limit=_CARD_PAGE_SIZE,
            offset=page * _CARD_PAGE_SIZE,
        )
        lines = [f"⭐ **收藏夹** · 第 `{page + 1}/{total_pages}` 页 · 共 `{total}`", "──────────"]
        if not jobs:
            lines.append("还没有收藏的作品。")
        rows: list[list] = []
        for index, job in enumerate(jobs, start=1):
            card = await self._result_service().build(job)
            size = sum(int(item.size_bytes) for item in job.items)
            label = await self._job_label(job)
            lines.append(
                f"{index}. {self._job_state_icon(job.state)} {label} · "
                f"{self._job_media_counts(job)} · {self._human_bytes(size)}"
            )
            if card.telegram_state in {"revoked", "partially_revoked"}:
                lines.append("   ↩️ 已撤销" if card.telegram_state == "revoked" else "   ⚠️ 部分撤销")
            if job.state == JobState.FAILED and job.error_code in {
                "publish_partial",
                "publish_uncertain",
            }:
                lines.append("   🛡️ 需人工核对")
            rows.append(
                [
                    Button.inline(
                        f"{index} 📋 结果", self._callback_data("result", job.id)
                    ),
                    Button.inline(
                        f"{index} ★ 取消", self._callback_data("fav", job.id)
                    ),
                ]
            )
        nav: list = []
        if page > 0:
            nav.append(Button.inline("⬅️", f"ui:favorites:{page - 1}".encode()))
        nav.append(Button.inline("🔄", f"ui:favorites:{page}".encode()))
        if page + 1 < total_pages:
            nav.append(Button.inline("➡️", f"ui:favorites:{page + 1}".encode()))
        if nav:
            rows.append(nav)
        rows.append([Button.inline("🏠 首页", b"ui:home"), Button.inline("📋 我的任务", b"ui:jobs")])
        return "\n".join(lines), rows
