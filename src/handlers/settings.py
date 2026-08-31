"""Bot/settings commands, callbacks and input interactions."""

from __future__ import annotations

import logging
import re
from typing import Any

from telethon import Button, events

from ..views import (
    BackupAttemptDetailView,
    BackupAttemptListItemView,
    BackupAttemptPageView,
    BackupFileItemView,
    HomeViewState,
    backup_attempt_detail_view,
    backup_attempt_page_view,
    MODE_NAMES,
    WebDavConfigViewState,
    home_button,
    home_view,
    mode_buttons,
    reply_keyboard,
    stats_view,
    webdav_cfg_fields_view,
    webdav_probe_view,
    webdav_write_confirm_view,
    webdav_write_result_view,
    webdav_cfg_view,
)
from .common import HandlerContext


logger = logging.getLogger(__name__)


def _webdav_state(ctx: HandlerContext) -> WebDavConfigViewState:
    cfg = ctx.backup.config_snapshot()
    return WebDavConfigViewState(
        enabled=bool(cfg.get("enabled")),
        url=cfg.get("url") or "",
        user=cfg.get("user") or "",
        has_password=bool(cfg.get("pass")),
        path=cfg.get("path") or "",
        retry=cfg.get("retry"),
    )


def _webdav_health(ctx: HandlerContext) -> str:
    cfg = ctx.backup.config_snapshot()
    if not cfg.get("enabled"):
        return "未启用"
    logs = list(ctx.backup.logs_snapshot().values())
    if not logs:
        return "正常"
    latest = max(
        (item for item in logs if isinstance(item, dict)),
        key=lambda item: float(item.get("ts") or 0),
        default=None,
    )
    if latest is None:
        return "正常"
    files = latest.get("files") or []
    if any(isinstance(item, dict) and item.get("status") not in ("ok", "deleted") for item in files):
        return "有待处理备份"
    return "正常"


async def _home(ctx: HandlerContext, user_id: int) -> tuple[str, list]:
    snapshot = await ctx.queue.home_snapshot(user_id)
    snapshot.update(
        webdav_enabled=bool(ctx.backup.get_config("enabled")),
        webdav_health=_webdav_health(ctx),
    )
    return home_view(HomeViewState(**snapshot))


async def _webdav_attempt_page(ctx: HandlerContext, page: int) -> tuple[str, list]:
    data = await ctx.backup.attempt_page(page)
    items = tuple(
        BackupAttemptListItemView(
            attempt_id=int(item["id"]),
            legacy_seq=item.get("legacy_seq"),
            state=str(item.get("state") or "unknown"),
            remote_dir=str(item.get("remote_dir") or ""),
            total_files=int(item.get("total_files") or 0),
            succeeded_files=int(item.get("succeeded_files") or 0),
            failed_files=int(item.get("failed_files") or 0),
            total_bytes=int(item.get("total_bytes") or 0),
            created_at=float(item.get("created_at") or 0),
            error_code=str(item.get("error_code") or ""),
        )
        for item in data["items"]
    )
    return backup_attempt_page_view(
        BackupAttemptPageView(
            page=int(data["page"]), pages=int(data["pages"]), total=int(data["total"]), items=items
        )
    )


async def _webdav_attempt_detail(ctx: HandlerContext, attempt_id: int, page: int) -> tuple[str, list] | None:
    data = await ctx.backup.attempt_detail_page(attempt_id, page)
    if data is None:
        return None
    attempt = data["attempt"]
    files = tuple(
        BackupFileItemView(
            file_id=int(item["id"]),
            remote_name=str(item.get("remote_name") or ""),
            size_bytes=int(item.get("size_bytes") or 0),
            state=str(item.get("state") or "unknown"),
            bytes_done=int(item.get("bytes_done") or 0),
            error_code=str(item.get("error_code") or ""),
        )
        for item in data["files"]
    )
    return backup_attempt_detail_view(
        BackupAttemptDetailView(
            attempt_id=int(attempt["id"]),
            legacy_seq=attempt.get("legacy_seq"),
            state=str(attempt.get("state") or "unknown"),
            remote_dir=str(attempt.get("remote_dir") or ""),
            retry_count=int(attempt.get("retry_count") or 0),
            next_retry_at=attempt.get("next_retry_at"),
            error_code=str(attempt.get("error_code") or ""),
            page=int(data["page"]), pages=int(data["pages"]), total=int(data["total"]), files=files,
        )
    )


def register_setting_commands(ctx: HandlerContext) -> None:
    @ctx.client.on(events.NewMessage(pattern="/start$"))
    async def on_start(event: events.NewMessage.Event) -> None:
        logger.info("CMD /start from %s", event.sender_id)
        if not ctx.authorized(event):
            return
        text, buttons = await _home(ctx, event.sender_id)
        await ctx.respond(
            event,
            text,
            buttons=buttons,
            auto_delete=False,
        )

    @ctx.client.on(events.NewMessage(pattern="/about$"))
    async def on_about(event: events.NewMessage.Event) -> None:
        logger.info("CMD /about from %s", event.sender_id)
        if not ctx.authorized(event):
            return
        await ctx.respond(event, ctx.about_text)

    @ctx.client.on(events.NewMessage(pattern="/stats$"))
    async def on_stats(event: events.NewMessage.Event) -> None:
        logger.info("CMD /stats from %s", event.sender_id)
        if not ctx.authorized(event):
            return
        if ctx.stats is None:
            await ctx.respond(event, "📊 运行状态暂不可用", auto_delete=False)
            return
        text, buttons = stats_view(await ctx.stats.snapshot())
        await ctx.respond(event, text, buttons=buttons, auto_delete=False)

    @ctx.client.on(events.NewMessage(pattern="/diag$"))
    async def on_diag(event: events.NewMessage.Event) -> None:
        logger.info("CMD /diag from %s", event.sender_id)
        if not ctx.authorized(event):
            return
        if ctx.stats is None:
            await ctx.respond(event, "🧾 诊断暂不可用", auto_delete=False)
            return
        await ctx.respond(
            event,
            await ctx.stats.diagnostics_text(),
            buttons=home_button(),
            auto_delete=False,
        )

    @ctx.client.on(events.NewMessage(pattern="/health$"))
    async def on_health(event: events.NewMessage.Event) -> None:
        logger.info("CMD /health from %s", event.sender_id)
        if not ctx.authorized(event):
            return
        if ctx.stats is None:
            await ctx.respond(event, "🩺 健康检查暂不可用", auto_delete=False)
            return
        await ctx.respond(
            event,
            await ctx.stats.health_text(),
            buttons=home_button(),
            auto_delete=False,
        )

    @ctx.client.on(events.NewMessage(pattern="/mode$"))
    async def on_mode(event: events.NewMessage.Event) -> None:
        logger.info("CMD /mode from %s", event.sender_id)
        if not ctx.authorized(event):
            return
        mode = ctx.queue.spoiler_mode(event.sender_id)
        await ctx.respond(
            event,
            f"当前 18+ 模式：{MODE_NAMES[mode]}\n请选择新的处理方式：",
            buttons=mode_buttons(),
            auto_delete=False,
        )

    @ctx.client.on(events.NewMessage(pattern=r"/webdav(\s|$)"))
    async def on_webdav(event: events.NewMessage.Event) -> None:
        logger.info("CMD /webdav from %s", event.sender_id)
        if not ctx.authorized(event):
            return
        parts = event.raw_text.strip().split(maxsplit=2)
        if len(parts) == 1:
            ctx.interactions.cancel(event.sender_id, "webdav")
            text, buttons = webdav_cfg_view(_webdav_state(ctx))
            await ctx.respond(event, text, buttons=buttons, auto_delete=False)
            return
        key = parts[1].lower()
        val = parts[2] if len(parts) > 2 else ""
        field_map = {
            "on": ("enabled", True),
            "enable": ("enabled", True),
            "off": ("enabled", False),
            "disable": ("enabled", False),
            "url": ("url", val),
            "user": ("user", val),
            "pass": ("pass", val),
            "path": ("path", val),
            "retry": ("retry", int(val) if val.isdigit() else None),
        }
        if key not in field_map:
            await ctx.respond(
                event,
                f"❌ 未知配置项: {key}（可用: on/off/url/user/pass/path/retry）",
                auto_delete=False,
            )
            return
        field, value = field_map[key]
        if value is None:
            await ctx.respond(
                event,
                f"❌ 参数无效: /webdav {key} <值>",
                auto_delete=False,
            )
            return
        if field == "path" and value and not value.startswith("/"):
            value = "/" + value
        ctx.backup.set_config(field, value)
        status = "✅ 已启用" if ctx.backup.get_config("enabled") else "⛔ 已停用"
        await ctx.respond(
            event,
            f"✅ 已更新 WebDAV {field}\n当前状态：{status}",
            auto_delete=False,
        )

    @ctx.client.on(events.NewMessage(pattern="/webdavlogs$"))
    async def on_webdavlogs(event: events.NewMessage.Event) -> None:
        logger.info("CMD /webdavlogs from %s", event.sender_id)
        if not ctx.authorized(event):
            return
        ctx.interactions.cancel(event.sender_id, "webdav")
        if ctx.backup.repository is not None:
            text, buttons = await _webdav_attempt_page(ctx, 0)
        else:
            text, buttons = ctx.pipeline._webdav_logs_view()
        await ctx.respond(event, text, buttons=buttons, auto_delete=False)

async def callback_mode(ctx: HandlerContext, event: Any, data: str) -> None:
    mode = data.split(":", 1)[1]
    if mode not in MODE_NAMES:
        await ctx.answer(event, "无效操作")
        return
    ctx.queue.set_spoiler_mode(event.sender_id, mode)
    await ctx.answer(event, f"已设置：{MODE_NAMES[mode]}")
    await ctx.edit(
        event,
        f"✅ 已设置 18+ 模式：{MODE_NAMES[mode]}",
        buttons=home_button(),
    )


async def callback_home(ctx: HandlerContext, event: Any, data: str) -> None:
    await ctx.answer(event)
    action = data.split(":", 1)[1] if ":" in data else "r"
    if action == "r":
        text, buttons = await _home(ctx, event.sender_id)
        await ctx.edit(event, text, buttons=buttons)
        return
    if action == "begin":
        session = ctx.queue.session(event.sender_id)
        if session is None:
            ctx.queue.begin_session(event.sender_id)
            await ctx.answer(event, "合集会话已开始")
        else:
            await ctx.answer(event, "合集会话已在进行中")
        text, buttons = await _home(ctx, event.sender_id)
        await ctx.edit(event, text, buttons=buttons)
        return
    if action == "end":
        if not ctx.queue.has_session(event.sender_id):
            await ctx.answer(event, "当前没有进行中的合集")
        else:
            try:
                count = await ctx.queue.finalize_session(event.sender_id, event.chat_id)
                await ctx.answer(event, f"已结束合集，共 {count} 个媒体")
            except Exception:
                logger.exception("Home session finalize failed")
                await ctx.answer(event, "结束合集失败")
        text, buttons = await _home(ctx, event.sender_id)
        await ctx.edit(event, text, buttons=buttons)
        return
    if action == "q":
        from .jobs import _durable_queue

        text, buttons = await _durable_queue(ctx, event.sender_id, "all", 0)
        await ctx.edit(event, text, buttons=buttons)
        return
    if action == "f":
        from .jobs import _failure_center

        text, buttons = await _failure_center(ctx, event.sender_id, 0)
        await ctx.edit(event, text, buttons=buttons)
        return
    if action == "w":
        text, buttons = webdav_cfg_view(_webdav_state(ctx))
        await ctx.edit(event, text, buttons=buttons)
        return
    if action == "s":
        mode = MODE_NAMES[ctx.queue.spoiler_mode(event.sender_id)]
        progress = "开启" if ctx.queue.progress_enabled(event.sender_id) else "关闭"
        text = (
            "⚙️ 设置\n──────────\n"
            f"🔞 18+ 模式：{mode}\n"
            f"📊 任务进度：{progress}\n"
            f"☁️ WebDAV：{'启用' if ctx.backup.get_config('enabled') else '停用'}\n"
            "──────────\n静态 .env 配置需重启后生效。"
        )
        buttons = [
            [Button.inline("🔞 18+ 模式", "h:mode"), Button.inline("📊 切换进度", "toggle_progress")],
            [Button.inline("☁️ WebDAV", "h:w"), Button.inline("🌐 代理", "h:p")],
            home_button(),
        ]
        await ctx.edit(event, text, buttons=buttons)
        return
    if action == "mode":
        mode = ctx.queue.spoiler_mode(event.sender_id)
        await ctx.edit(
            event,
            f"🔞 18+ 模式\n──────────\n当前：{MODE_NAMES[mode]}\n请选择新的处理方式：",
            buttons=[*mode_buttons(), home_button()],
        )
        return
    if action == "p":
        text, buttons = ctx.pipeline._proxy_view()
        await ctx.edit(event, text, buttons=buttons)
        return
    if action == "status":
        if ctx.stats is None:
            await ctx.edit(event, "📊 运行状态暂不可用", buttons=home_button())
            return
        text, buttons = stats_view(await ctx.stats.snapshot())
        await ctx.edit(event, text, buttons=buttons)
        return
    if action == "diag":
        if ctx.stats is None:
            await ctx.edit(event, "🧾 诊断暂不可用", buttons=home_button())
            return
        await ctx.edit(
            event,
            await ctx.stats.diagnostics_text(),
            buttons=home_button(),
        )
        return
    if action == "health":
        if ctx.stats is None:
            await ctx.edit(event, "🩺 健康检查暂不可用", buttons=home_button())
            return
        await ctx.edit(
            event,
            await ctx.stats.health_text(),
            buttons=home_button(),
        )
        return
    if action == "help":
        text = "❓ 帮助中心\n──────────\n请选择主题："
        buttons = [
            [Button.inline("📥 收集与发布", "h:help:collect"), Button.inline("📋 队列与任务", "h:help:queue")],
            [Button.inline("☁️ WebDAV 备份", "h:help:webdav"), Button.inline("🌐 URL 与代理", "h:help:proxy")],
            [Button.inline("⚙️ 设置说明", "h:help:settings"), Button.inline("🛠 故障排查", "h:help:trouble")],
            home_button(),
        ]
        await ctx.edit(event, text, buttons=buttons)
        return
    if action.startswith("help:"):
        topic = action.split(":", 1)[1]
        help_text = {
            "collect": "📥 收集与发布\n──────────\n转发媒体会进入队列；需要合集时先开始合集，结束后统一发布。相册与合集会保持原顺序。",
            "queue": "📋 队列与任务\n──────────\n队列页支持分页、筛选和任务详情。取消、删除缓存、撤销发布都会先要求二次确认。",
            "webdav": "☁️ WebDAV 备份\n──────────\nTelegram 发布与 WebDAV 备份相互独立。备份失败不会撤回已发布消息，可在备份管理中重试。",
            "proxy": "🌐 URL 与代理\n──────────\nURL 下载失败时可按配置自动切换 HTTP 代理。代理凭证只显示脱敏摘要。",
            "settings": "⚙️ 设置说明\n──────────\n18+ 模式与进度显示可即时修改；.env 中的静态配置需要重启后生效。",
            "trouble": "🛠 故障排查\n──────────\n先查看失败中心的任务详情。缓存存在时可能可直接重试；源失效、权限或配置错误需要按详情提示处理。",
        }.get(topic)
        if help_text is None:
            await ctx.answer(event, "帮助主题不存在")
            return
        await ctx.edit(
            event,
            help_text,
            buttons=[[Button.inline("⬅️ 帮助首页", "h:help")], home_button()],
        )
        return
    await ctx.answer(event, "操作已过期，请刷新")


async def callback_webdav_config(ctx: HandlerContext, event: Any, data: str) -> None:
    field = data.split(":", 1)[1]
    if field == "logs":
        await ctx.answer(event, "上传记录")
        if ctx.backup.repository is not None:
            text, buttons = await _webdav_attempt_page(ctx, 0)
        else:
            text, buttons = ctx.pipeline._webdav_logs_view()
        await ctx.edit(event, text, buttons=buttons)
        return
    if field == "edit":
        await ctx.answer(event, "修改配置")
        text, buttons = webdav_cfg_fields_view(_webdav_state(ctx))
        await ctx.edit(event, text, buttons=buttons)
        return
    if field == "test":
        await ctx.answer(event, "正在测试 WebDAV 读取连接…")
        cfg = ctx.backup.config_snapshot()
        if not cfg.get("url") or not cfg.get("path"):
            await ctx.edit(
                event,
                "🧪 WebDAV 连接测试\n────────────────────────\n❌ 请先配置地址和路径",
                buttons=[[Button.inline("⬅️ 返回 WebDAV", "wd_cfg:back")], home_button()],
            )
            return
        result = await ctx.backup.test_connection()
        text, buttons = webdav_probe_view(result)
        await ctx.edit(event, text, buttons=buttons)
        return
    if field == "wtest":
        cfg = ctx.backup.config_snapshot()
        if not cfg.get("url") or not cfg.get("path"):
            await ctx.answer(event, "请先配置 WebDAV 地址和路径")
            return
        operation = ctx.operations.create(
            user_id=event.sender_id,
            action="webdav_write_test",
            job_id=0,
            expected_revision=0,
        )
        text, buttons = webdav_write_confirm_view(operation.operation_id)
        await ctx.edit(event, text, buttons=buttons)
        return
    if field == "back":
        text, buttons = webdav_cfg_view(_webdav_state(ctx))
        await ctx.edit(event, text, buttons=buttons)
        return
    if field in ("on", "off"):
        ctx.backup.set_enabled(field == "on")
        await ctx.answer(event, "✅ 已启用" if field == "on" else "⛔ 已停用")
        text, buttons = webdav_cfg_view(_webdav_state(ctx))
        await ctx.edit(event, text, buttons=buttons)
        return
    if field == "cancel":
        ctx.interactions.cancel(event.sender_id, "webdav")
        await ctx.answer(event, "已取消")
        return
    if field in ("url", "user", "pass", "path", "retry"):
        ctx.interactions.start(event.sender_id, "webdav", field)
        current = ctx.backup.get_config(field)
        if field == "pass":
            current = "***" if current else "（空）"
        await ctx.answer(event, "请直接回复新值")
        await ctx.edit(
            event,
            f"✏️ 请输入新的 WebDAV {field}"
            f"（当前：{current or '（空）'}）\n"
            f"直接回复即可；回复 /取消 取消修改",
            buttons=[Button.inline("❌ 取消", "wd_cfg:cancel")],
        )
        return
    await ctx.answer(event, "无效操作")


async def callback_webdav_write_test(ctx: HandlerContext, event: Any, data: str) -> None:
    parts = data.split(":")
    if len(parts) != 3 or parts[1] not in {"y", "n"} or not parts[2].isdigit():
        await ctx.answer(event, "操作已过期，请刷新")
        return
    operation_id = int(parts[2])
    if parts[1] == "n":
        if ctx.operations.discard(operation_id, user_id=event.sender_id):
            await ctx.answer(event, "已取消写入测试")
        else:
            await ctx.answer(event, "操作已过期，请刷新")
        text, buttons = webdav_cfg_view(_webdav_state(ctx))
        await ctx.edit(event, text, buttons=buttons)
        return
    operation = ctx.operations.consume(operation_id, user_id=event.sender_id)
    if operation is None or operation.action != "webdav_write_test":
        await ctx.answer(event, "操作已过期，请刷新")
        return
    await ctx.answer(event, "正在执行写入/校验/清理测试…")
    result = await ctx.backup.test_write()
    text, buttons = webdav_write_result_view(result)
    await ctx.edit(event, text, buttons=buttons)


async def callback_webdav_retry(ctx: HandlerContext, event: Any, data: str) -> None:
    key = data.split(":", 1)[1]
    await ctx.answer(event, await ctx.backup.retry(key))
    text, buttons = ctx.pipeline._webdav_logs_view()
    await ctx.edit(event, text, buttons=buttons)


async def callback_webdav_durable(ctx: HandlerContext, event: Any, data: str) -> None:
    parts = data.split(":")
    if len(parts) < 3:
        await ctx.answer(event, "无效操作")
        return
    action = parts[1]
    if action == "p" and parts[2].isdigit():
        text, buttons = await _webdav_attempt_page(ctx, int(parts[2]))
        await ctx.edit(event, text, buttons=buttons)
        return
    if action == "a" and len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
        rendered = await _webdav_attempt_detail(ctx, int(parts[2]), int(parts[3]))
        if rendered is None:
            await ctx.answer(event, "记录不存在或已过期")
            return
        await ctx.edit(event, rendered[0], buttons=rendered[1])
        return
    if action == "fr" and parts[2].isdigit():
        await ctx.answer(event, "正在重试文件…")
        result = await ctx.backup.retry_file(int(parts[2]))
        await ctx.answer(event, result)
        row = await ctx.backup.repository.backup_file_detail(int(parts[2])) if ctx.backup.repository is not None else None
        if row is not None:
            rendered = await _webdav_attempt_detail(ctx, int(row["attempt_id"]), 0)
            if rendered is not None:
                await ctx.edit(event, rendered[0], buttons=rendered[1])
        return
    if action == "ar" and parts[2].isdigit():
        attempt_id = int(parts[2])
        await ctx.answer(event, "正在重试失败文件…")
        result = await ctx.backup.retry_attempt(attempt_id)
        await ctx.answer(event, result)
        rendered = await _webdav_attempt_detail(ctx, attempt_id, 0)
        if rendered is not None:
            await ctx.edit(event, rendered[0], buttons=rendered[1])
        return
    await ctx.answer(event, "无效操作")


async def callback_webdav_delete(ctx: HandlerContext, event: Any, data: str) -> None:
    key = data.split(":", 1)[1]
    await ctx.answer(event, await ctx.backup.delete(key))
    text, buttons = ctx.pipeline._webdav_logs_view()
    await ctx.edit(event, text, buttons=buttons)


async def callback_webdav_cache(ctx: HandlerContext, event: Any, data: str) -> None:
    seq_s = data.split(":", 1)[1]
    if not seq_s.isdigit():
        await ctx.answer(event, "无效操作")
        return
    await ctx.answer(event, "已开始上传，进度见新消息…")
    text, buttons = ctx.pipeline._webdav_logs_view()
    await ctx.edit(event, text, buttons=buttons)

    async def _bg() -> None:
        try:
            result = await ctx.backup.upload_cache(int(seq_s), event.sender_id)
            try:
                await event.respond(result)
            except Exception:
                pass
        except Exception as exc:
            logger.error("wd_cache_up 后台任务异常: %s", exc)

    ctx.spawn(_bg())


async def handle_webdav_input(ctx: HandlerContext, event: Any, session: Any) -> bool:
    text = (event.raw_text or "").strip()
    if text in ("/取消", "/cancel"):
        ctx.interactions.finish(event.sender_id, session.revision)
        await ctx.respond(event, "❌ 已取消修改", auto_delete=False)
        return True
    field = session.field
    value: Any = text
    if field == "retry":
        if not value.isdigit():
            await ctx.respond(
                event,
                "❌ 重试次数必须是数字，请重新输入",
                auto_delete=False,
            )
            return True
        value = int(value)
        if value < 0 or value > 10:
            await ctx.respond(
                event,
                "❌ 重试次数需在 0-10 之间，请重新输入",
                auto_delete=False,
            )
            return True
    elif field == "url":
        if not re.match(r"^https?://", value, re.IGNORECASE):
            await ctx.respond(
                event,
                "❌ 地址需以 http:// 或 https:// 开头，请重新输入",
                auto_delete=False,
            )
            return True
    elif field == "path" and value and not value.startswith("/"):
        value = "/" + value
    if field in ("url", "user", "pass", "path") and not value:
        await ctx.respond(event, "❌ 内容不能为空，请重新输入", auto_delete=False)
        return True
    ctx.backup.set_config(field, value)
    ctx.interactions.finish(event.sender_id, session.revision)
    shown = "***" if field == "pass" else value
    await ctx.respond(
        event,
        f"✅ 已更新 WebDAV {field}：{shown}",
        auto_delete=False,
    )
    view_text, buttons = webdav_cfg_fields_view(_webdav_state(ctx))
    await ctx.respond(event, view_text, buttons=buttons, auto_delete=False)
    return True


def register_setting_callbacks(router: Any) -> None:
    router.prefix("h:", callback_home)
    router.prefix("wd_cfg:", callback_webdav_config)
    router.prefix("wd_retry:", callback_webdav_retry)
    router.prefix("wd_del:", callback_webdav_delete)
    router.prefix("wd_cache_up:", callback_webdav_cache)
    router.prefix("wd_w:", callback_webdav_write_test)
    router.prefix("wd:", callback_webdav_durable)
    router.prefix("mode:", callback_mode)

