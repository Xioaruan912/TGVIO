from __future__ import annotations

from telethon import Button

from tgvio.application.bulk_retry import BulkRetryPlan, BulkRetryService
from tgvio.application.operation_tokens import OperationTokenInvalidError
from tgvio.domain.job import JobState


RETRY_ALL_DATA = b"ui:retry-all"
_CONFIRM_PREFIX = "ui:retry-all-go:"
_ACTION = "retry_all"


class BotUIRetryAllMixin:
    """One button that starts every safe retry instead of one Job at a time."""

    _bulk_retry: BulkRetryService | None = None

    async def _retry_all_plan(self, owner_id: int) -> BulkRetryPlan | None:
        if self._bulk_retry is None:
            return None
        plan = await self._bulk_retry.plan(owner_id)
        return None if plan.empty else plan

    @staticmethod
    def _retry_all_button(plan: BulkRetryPlan | None) -> list:
        return [] if plan is None else [[Button.inline("🔁 全部重试", RETRY_ALL_DATA)]]

    async def _handle_retry_all_callback(self, event, owner_id: int, action: str) -> bool:
        if action == RETRY_ALL_DATA.decode():
            await self._show_retry_all_confirmation(event, owner_id)
            return True
        if action.startswith(_CONFIRM_PREFIX):
            await self._run_retry_all(event, owner_id, action[len(_CONFIRM_PREFIX) :])
            return True
        return False

    async def _show_retry_all_confirmation(self, event, owner_id: int) -> None:
        plan = await self._retry_all_plan(owner_id)
        if plan is None or self._operation_tokens is None:
            await self._edit_page(
                event,
                "✅ **没有需要重试的内容**\n\n出问题的任务会先自动重试，不用手动处理。",
                [[Button.inline("🏠 首页", b"ui:home")]],
            )
            return
        operation = await self._operation_tokens.issue(
            owner_id=owner_id,
            action=_ACTION,
            resource_type="owner",
            resource_id=str(owner_id),
            expected_revision=0,
            payload=plan.payload(),
        )
        await self._edit_page(
            event,
            self._retry_all_summary(plan),
            [
                [Button.inline("✅ 开始重试", f"{_CONFIRM_PREFIX}{operation.token}".encode())],
                [Button.inline("返回", b"ui:failures:0")],
            ],
        )

    @staticmethod
    def _retry_all_summary(plan: BulkRetryPlan) -> str:
        lines = ["🔁 **全部重试**", "", "将会："]
        if plan.job_ids:
            lines.append(f"• 重新处理 {len(plan.job_ids)} 个失败的任务")
        if plan.skipped_parent_ids:
            lines.append(
                f"• 补发 {plan.skipped_items} 个之前没下载到的文件"
                f"（来自 {len(plan.skipped_parent_ids)} 个任务）"
            )
        if plan.archive_job_ids:
            lines.append(f"• 重新备份 {len(plan.archive_job_ids)} 个任务到云端")
        if plan.blocked_job_ids:
            lines.extend(
                [
                    "",
                    f"另有 {len(plan.blocked_job_ids)} 个任务不会重试："
                    "原消息已删除，或可能已经发出一部分（重发会重复）。",
                ]
            )
        lines.extend(["", "已经发出去的内容不会重复发送。"])
        return "\n".join(lines)

    async def _run_retry_all(self, event, owner_id: int, token: str) -> None:
        plan = await self._retry_all_plan(owner_id)
        try:
            if plan is None or self._operation_tokens is None:
                raise OperationTokenInvalidError("nothing to retry")
            await self._operation_tokens.consume(
                token=token,
                owner_id=owner_id,
                action=_ACTION,
                resource_type="owner",
                resource_id=str(owner_id),
                expected_revision=0,
                payload=plan.payload(),
            )
        except OperationTokenInvalidError:
            await self._edit_page(
                event,
                "情况已经变了（可能有任务刚自动恢复）。请再看一下最新列表。",
                [[Button.inline("查看最新", RETRY_ALL_DATA)], [Button.inline("🏠 首页", b"ui:home")]],
            )
            return
        result = await self._bulk_retry.run(owner_id, plan)
        if self._schedule_job is not None:
            for decision in result.retried:
                if decision.target_state == JobState.RECEIVED or self._settings.publish_enabled:
                    self._schedule_job(decision.job, chat_id=event.chat_id)
            for child in result.recovery_children:
                self._schedule_job(child, chat_id=event.chat_id)
        started = len(result.retried) + len(result.recovery_children) + result.archives_requeued
        lines = [f"✅ **已开始重试 {started} 项**", "", "进度会在任务列表里更新，完成后会通知你。"]
        if result.unchanged:
            lines.append(f"有 {result.unchanged} 项刚刚已经变了状态，这次跳过了。")
        await self._edit_page(
            event,
            "\n".join(lines),
            [[Button.inline("📋 看进度", b"ui:jobs:running:0")], [Button.inline("🏠 首页", b"ui:home")]],
        )
