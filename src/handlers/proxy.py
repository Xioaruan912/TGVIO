"""HTTP proxy command, callback and input interaction handlers."""

from __future__ import annotations

from typing import Any

from telethon import Button, events

from .common import HandlerContext


def register_proxy_command(ctx: HandlerContext) -> None:
    @ctx.client.on(events.NewMessage(pattern="/proxy$"))
    async def on_proxy(event: events.NewMessage.Event) -> None:
        if not ctx.authorized(event):
            return
        ctx.interactions.cancel(event.sender_id, "proxy")
        text, buttons = ctx.pipeline._proxy_view()
        await ctx.respond(event, text, buttons=buttons, auto_delete=False)


async def callback_proxy(ctx: HandlerContext, event: Any, data: str) -> None:
    field = data.split(":", 1)[1]

    async def refresh(text: str, buttons: object) -> None:
        await ctx.edit(event, text, buttons=buttons)

    if field == "add":
        ctx.interactions.start(event.sender_id, "proxy", "add")
        await ctx.answer(event, "请输入代理地址")
        await refresh(
            "➕ 请输入 HTTP 代理地址（直接回复即可）：\n\n"
            "格式：\n"
            "  http://host:port\n"
            "  http://user:pass@host:port\n\n"
            "回复 /取消 取消添加",
            [Button.inline("❌ 取消", "proxy:cancel")],
        )
        return
    if field == "cancel":
        ctx.interactions.cancel(event.sender_id, "proxy")
        await ctx.answer(event, "已取消")
        text, buttons = ctx.pipeline._proxy_view()
        await refresh(text, buttons)
        return
    if field == "auto":
        enabled = ctx.proxy.toggle_auto()
        await ctx.answer(event, "自动切换已开启" if enabled else "自动切换已关闭")
        text, buttons = ctx.pipeline._proxy_view()
        await refresh(text, buttons)
        return
    if field == "direct":
        ok = await ctx.proxy.apply(-1)
        await ctx.answer(event, "已切回直连" if ok else "切换失败")
        text, buttons = ctx.pipeline._proxy_view()
        await refresh(text, buttons)
        return
    if field == "list":
        text, buttons = ctx.pipeline._proxy_list_view()
        await refresh(text, buttons)
        return
    if field == "back":
        text, buttons = ctx.pipeline._proxy_view()
        await refresh(text, buttons)
        return
    if field.startswith("use:"):
        try:
            idx = int(field.split(":", 1)[1])
        except (ValueError, IndexError):
            await ctx.answer(event, "无效操作")
            return
        ok = await ctx.proxy.apply(idx)
        await ctx.answer(event, "✅ 已切换" if ok else "❌ 切换失败")
        text, buttons = ctx.pipeline._proxy_view()
        await refresh(text, buttons)
        return
    if field.startswith("test:"):
        try:
            idx = int(field.split(":", 1)[1])
        except (ValueError, IndexError):
            await ctx.answer(event, "无效操作")
            return
        proxies = ctx.proxy.proxies()
        if idx < 0 or idx >= len(proxies):
            await ctx.answer(event, "代理不存在")
            return
        url = proxies[idx].get("url", "")
        await ctx.answer(event, "🧪 测试中…")

        async def do_proxy_test() -> None:
            ok = await ctx.proxy.test(url)
            try:
                await event.respond(
                    f"🧪 代理 #{idx + 1}："
                    f"{'✅ 可用' if ok else '❌ 不可用'}\n"
                    f"{ctx.proxy.label(idx)}"
                )
            except Exception:
                pass

        ctx.spawn(do_proxy_test())
        return
    if field.startswith("del:"):
        try:
            idx = int(field.split(":", 1)[1])
        except (ValueError, IndexError):
            await ctx.answer(event, "无效操作")
            return
        removed = await ctx.proxy.remove(idx)
        if removed is None:
            await ctx.answer(event, "代理不存在")
            return
        parsed = ctx.proxy.parse(removed.get("url", ""))
        if parsed is None:
            removed_label = f"代理 #{idx + 1}"
        else:
            _, host, port, username, _password, _rdns = parsed
            removed_label = f"http://{'***@' if username else ''}{host}:{port}"
        await ctx.answer(event, f"已删除代理 {removed_label}")
        text, buttons = ctx.pipeline._proxy_list_view()
        await refresh(text, buttons)
        return
    await ctx.answer(event, "无效操作")


async def handle_proxy_input(ctx: HandlerContext, event: Any, session: Any) -> bool:
    text = (event.raw_text or "").strip()
    if text in ("/取消", "/cancel"):
        ctx.interactions.finish(event.sender_id, session.revision)
        await ctx.respond(event, "❌ 已取消添加代理", auto_delete=False)
        view_text, buttons = ctx.pipeline._proxy_view()
        await ctx.respond(event, view_text, buttons=buttons, auto_delete=False)
        return True

    ctx.interactions.finish(event.sender_id, session.revision)
    if ctx.proxy.parse(text) is None:
        await ctx.respond(
            event,
            "❌ 代理格式无效，应形如 http://host:port 或 http://user:pass@host:port\n"
            "重新发 /proxy 再试",
            auto_delete=False,
        )
        return True
    if ctx.proxy.contains(text):
        await ctx.respond(event, "⚠️ 该代理已存在", auto_delete=False)
        view_text, buttons = ctx.pipeline._proxy_view()
        await ctx.respond(event, view_text, buttons=buttons, auto_delete=False)
        return True
    if not await ctx.proxy.test(text):
        await ctx.respond(
            event,
            "⚠️ 该代理测试连通失败，仍要添加请确认代理可用；已跳过添加",
            auto_delete=False,
        )
        view_text, buttons = ctx.pipeline._proxy_view()
        await ctx.respond(event, view_text, buttons=buttons, auto_delete=False)
        return True
    index = ctx.proxy.add(text)
    await ctx.respond(
        event,
        f"✅ 已添加代理：{ctx.proxy.label(index)}",
        auto_delete=False,
    )
    view_text, buttons = ctx.pipeline._proxy_view()
    await ctx.respond(event, view_text, buttons=buttons, auto_delete=False)
    return True


def register_proxy_callbacks(router: Any) -> None:
    router.prefix("proxy:", callback_proxy)

