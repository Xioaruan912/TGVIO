"""S1 source-profile management and realtime intake."""

from __future__ import annotations

import logging
import time
from typing import Any

from telethon import Button, events
from telethon.utils import get_peer_id

from ..repository import RepositoryError
from ..services.source_runtime import (
    SourceProfileRuntime,
    is_supported_source_media,
    probe_source_access,
)
from ..views.source_profiles import SourceProfileView, source_profile_detail_view, source_profiles_view
from .common import HandlerContext


logger = logging.getLogger(__name__)


async def _view(ctx: HandlerContext, profile: Any) -> SourceProfileView:
    destination = await ctx.destinations.repository.get_destination_profile(profile.destination_profile_id)
    return SourceProfileView(
        profile_id=profile.id,
        name=profile.name,
        source_peer=profile.source_peer,
        destination_name=destination.name if destination is not None else "不可用",
        enabled=profile.enabled,
        spoiler_policy=profile.spoiler_policy,
        caption_policy=profile.caption_policy,
        backup_policy=profile.backup_policy,
        album_gather_seconds=profile.album_gather_seconds,
        sequential_video_gather_seconds=profile.sequential_video_gather_seconds,
    )


async def _list_view(ctx: HandlerContext) -> tuple[str, list]:
    if ctx.sources is None or ctx.destinations is None:
        return "📡 自动来源暂不可用", [[Button.inline("🏠 首页", "h:r")]]
    items = [await _view(ctx, item) for item in await ctx.sources.list_profiles()]
    return source_profiles_view(tuple(items))


async def _detail_view(ctx: HandlerContext, profile_id: int) -> tuple[str, list] | None:
    if ctx.sources is None:
        return None
    profile = await ctx.sources.get(profile_id)
    if profile is None:
        return None
    return source_profile_detail_view(await _view(ctx, profile))


async def handle_source_profile_input(ctx: HandlerContext, event: Any, session: Any) -> bool:
    if ctx.sources is None or ctx.destinations is None:
        return False
    field = str(session.field or "")
    if not field.startswith("add:"):
        return False
    try:
        destination_id = int(field.split(":", 1)[1])
    except (IndexError, ValueError):
        return False
    raw = str(event.raw_text or "").strip()
    if "|" not in raw:
        await ctx.respond(event, "格式：名称 | 来源频道，例如：资讯源 | @source")
        return True
    name, source_peer = (part.strip() for part in raw.split("|", 1))
    if not name or not source_peer:
        await ctx.respond(event, "名称和来源都不能为空")
        return True
    try:
        access = await probe_source_access(ctx.client, source_peer)
        if not access.ok or access.source_peer_id is None:
            await ctx.respond(event, f"来源验证失败：{access.message}（{access.code}）")
            return True
        profile = await ctx.sources.create_verified(
            name=name,
            source_peer=source_peer,
            source_peer_id=access.source_peer_id,
            destination_profile_id=destination_id,
            owner_user_id=event.sender_id,
        )
    except Exception as exc:
        logger.warning("Source profile creation failed: %s", exc.__class__.__name__)
        await ctx.respond(event, "来源保存失败：名称、来源或目的地配置冲突")
        return True
    ctx.interactions.cancel(event.sender_id, "source_profile")
    await ctx.respond(
        event,
        f"✅ 已创建来源 {profile.name}，当前保持禁用。请在 /sources 中确认后再启用。",
        auto_delete=False,
    )
    return True


def register_source_profile_handlers(ctx: HandlerContext) -> None:
    @ctx.client.on(events.NewMessage(pattern="/sources$", func=lambda event: event.is_private))
    async def on_sources(event: events.NewMessage.Event) -> None:
        if not ctx.authorized(event):
            return
        text, buttons = await _list_view(ctx)
        await ctx.respond(event, text, buttons=buttons, auto_delete=False)

    async def enqueue_source(profile: Any, messages: list[Any], event_ids: list[int]) -> None:
        if ctx.sources is None or ctx.destinations is None:
            return
        try:
            destination = await ctx.destinations.repository.get_destination_profile(profile.destination_profile_id)
            if destination is None or not destination.enabled:
                raise RepositoryError("destination_unavailable")
            snapshot = ctx.destinations.repository.destination_profile_snapshot(destination)
            if profile.backup_policy != "inherit":
                snapshot["backup_policy"] = profile.backup_policy
            spoiler = False
            if profile.spoiler_policy == "spoiler":
                spoiler = True
            elif profile.spoiler_policy == "rule":
                spoiler = str(snapshot.get("default_spoiler_mode") or "always_normal") == "always_spoiler"
            if profile.caption_policy == "strip":
                for message in messages:
                    try:
                        message.message = ""
                    except Exception:
                        pass
            kind = "album" if len(messages) > 1 else "media"
            seq = await ctx.queue.auto_enqueue(
                kind,
                messages[0] if len(messages) == 1 else None,
                messages if len(messages) > 1 else None,
                profile.owner_user_id,
                spoiler_override=spoiler,
                destination_profile_id=destination.id,
                destination_profile_snapshot=snapshot,
                allow_album_merge=False,
            )
            await ctx.sources.mark_enqueued(event_ids, seq)
        except Exception as exc:
            await ctx.sources.mark_failed(event_ids, exc.__class__.__name__)
            logger.warning("Source profile enqueue failed profile=%s code=%s", profile.id, exc.__class__.__name__)
            try:
                await ctx.client.send_message(
                    profile.owner_user_id,
                    f"⚠️ 自动中转失败：来源 {profile.name}，错误 {exc.__class__.__name__}",
                )
            except Exception:
                pass

    runtime = None
    if ctx.sources is not None and ctx.destinations is not None:
        runtime = SourceProfileRuntime(
            client=ctx.client,
            sources=ctx.sources,
            enqueue=enqueue_source,
        )
    ctx.pipeline.source_runtime = runtime

    @ctx.client.on(events.NewMessage(incoming=True, func=lambda event: not event.is_private))
    async def on_source_message(event: events.NewMessage.Event) -> None:
        if ctx.sources is None or ctx.destinations is None or runtime is None:
            return
        message = event.message
        peer_id = int(event.chat_id or get_peer_id(getattr(message, "peer_id", 0)))
        profile = await ctx.sources.get_enabled_by_peer(peer_id)
        if profile is None:
            return
        if not is_supported_source_media(message):
            await runtime.boundary(profile)
            return
        source_message_id = int(getattr(message, "id", 0) or 0)
        if not source_message_id:
            return
        grouped_id = getattr(message, "grouped_id", None)
        record, inserted = await ctx.sources.accept_event(
            profile=profile,
            source_message_id=source_message_id,
            grouped_id=int(grouped_id) if grouped_id is not None else None,
        )
        if not inserted:
            return
        await runtime.accept(
            profile,
            message,
            record.id,
            grouped_id=int(grouped_id) if grouped_id is not None else None,
            received_at=record.created_at,
        )


async def callback_source_profile(ctx: HandlerContext, event: Any, data: str) -> None:
    await ctx.answer(event)
    if ctx.sources is None or ctx.destinations is None:
        await ctx.edit(event, "📡 自动来源暂不可用")
        return
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "r"
    if action == "r":
        text, buttons = await _list_view(ctx)
        await ctx.edit(event, text, buttons=buttons)
        return
    if action == "add":
        destinations = await ctx.destinations.list_profiles(enabled_only=True)
        buttons = [[Button.inline(item.name[:28], f"sp:ad:{item.id}")] for item in destinations]
        buttons.append([Button.inline("⬅️ 返回", "sp:r")])
        await ctx.edit(event, "先选择自动中转的目的地：", buttons=buttons)
        return
    if action == "ad" and len(parts) >= 3 and parts[2].isdigit():
        destination_id = int(parts[2])
        destination = await ctx.destinations.repository.get_destination_profile(destination_id)
        if destination is None or not destination.enabled:
            await ctx.answer(event, "目的地不可用")
            return
        ctx.interactions.start(event.sender_id, "source_profile", f"add:{destination_id}", ttl=300)
        await ctx.edit(
            event,
            "请发送：名称 | 来源频道\n"
            "例如：资讯源 | @source\n\n"
            "创建时会验证 Bot 自身确实是来源成员；频道请先把 Bot 设为管理员。"
            "创建后仍保持禁用。",
            buttons=[Button.inline("⬅️ 返回", "sp:r")],
        )
        return
    if len(parts) < 3 or not parts[2].isdigit():
        return
    profile_id = int(parts[2])
    profile = await ctx.sources.get(profile_id)
    if profile is None:
        await ctx.answer(event, "来源不存在")
        return
    if action == "v":
        view = await _detail_view(ctx, profile_id)
        if view:
            await ctx.edit(event, view[0], buttons=view[1])
        return
    if action == "tg":
        if profile.enabled:
            result = await ctx.sources.set_enabled(profile_id, False)
            await ctx.answer(event, "已禁用" if result == "ok" else result)
        else:
            access = await probe_source_access(
                ctx.client,
                profile.source_peer,
                expected_peer_id=profile.source_peer_id,
            )
            if not access.ok:
                await ctx.answer(event, access.message)
                view = await _detail_view(ctx, profile_id)
                if view:
                    await ctx.edit(
                        event,
                        f"{view[0]}\n\n访问检查：❌ {access.message}（{access.code}）",
                        buttons=view[1],
                    )
                return
            result = await ctx.sources.update(
                profile_id,
                verified_at=time.time(),
                enabled=True,
            )
            await ctx.answer(event, "访问检查通过，已启用" if result == "ok" else result)
    elif action == "chk":
        access = await probe_source_access(
            ctx.client,
            profile.source_peer,
            expected_peer_id=profile.source_peer_id,
        )
        if access.ok:
            await ctx.sources.update(profile_id, verified_at=time.time())
        await ctx.answer(event, access.message)
        view = await _detail_view(ctx, profile_id)
        if view:
            icon = "✅" if access.ok else "❌"
            await ctx.edit(
                event,
                f"{view[0]}\n\n访问检查：{icon} {access.message}（{access.code}）",
                buttons=view[1],
            )
        return
    elif action == "spo":
        values = ["normal", "spoiler", "rule"]
        value = values[(values.index(profile.spoiler_policy) + 1) % len(values)]
        result = await ctx.sources.update(profile_id, spoiler_policy=value)
        await ctx.answer(event, value if result == "ok" else result)
    elif action == "cap":
        value = "strip" if profile.caption_policy == "preserve" else "preserve"
        result = await ctx.sources.update(profile_id, caption_policy=value)
        await ctx.answer(event, value if result == "ok" else result)
    elif action == "bp":
        values = ["inherit", "best_effort", "required"]
        value = values[(values.index(profile.backup_policy) + 1) % len(values)]
        result = await ctx.sources.update(profile_id, backup_policy=value)
        await ctx.answer(event, value if result == "ok" else result)
    elif action == "dst":
        destinations = await ctx.destinations.list_profiles(enabled_only=True)
        buttons = [[Button.inline(item.name[:28], f"sp:ds:{profile_id}:{item.id}")] for item in destinations]
        buttons.append([Button.inline("⬅️ 返回", f"sp:v:{profile_id}")])
        await ctx.edit(event, "选择新的目的地（只影响之后收到的新消息）：", buttons=buttons)
        return
    elif action == "ds" and len(parts) >= 4 and parts[3].isdigit():
        result = await ctx.sources.update(profile_id, destination_profile_id=int(parts[3]))
        await ctx.answer(event, "目的地已更新" if result == "ok" else result)
    view = await _detail_view(ctx, profile_id)
    if view:
        await ctx.edit(event, view[0], buttons=view[1])


def register_source_callbacks(router: Any) -> None:
    router.prefix("sp:", callback_source_profile)
