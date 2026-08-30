import unittest

from src.views import (
    MODE_NAMES,
    PendingQueueItemView,
    ProxyViewState,
    QueueItemView,
    QueueViewState,
    SESSION_BTN_BEGIN,
    SESSION_BTN_END,
    WebDavConfigViewState,
    mode_buttons,
    proxy_list_view,
    proxy_view,
    queue_view,
    reply_keyboard,
    webdav_cfg_fields_view,
    webdav_cfg_view,
)


def callback_data(buttons):
    return [button.data for row in buttons for button in row]


class ViewRenderingTests(unittest.TestCase):
    def test_queue_view_renders_from_immutable_state(self) -> None:
        state = QueueViewState(
            show_progress=True,
            active=(
                QueueItemView(
                    seq=10,
                    position=1,
                    state="download",
                    pct=25,
                    item=1,
                    items=4,
                ),
                QueueItemView(seq=11, position=2, state="paused"),
            ),
            pending=(PendingQueueItemView(seq=20, position=1, kind="album"),),
            has_sessions=True,
            session_media=3,
            session_texts=2,
        )

        text, buttons = queue_view(state)

        self.assertEqual(
            text,
            "📋 队列管理\n"
            "每个按钮带位置序号，对应下方第 N 位。\n\n"
            "▶ 进行中（2）\n"
            "队列第 1 位 ⬇ 下载 1/4 ██░░░░░░░░  25%\n"
            "队列第 2 位 ⏸ 已暂停\n\n"
            "❓ 待确认\n"
            "❓① 待确认（相册）\n\n"
            "📦 合集会话进行中：3 个媒体 · 2 条评论已收录（发 /end 结束并发布）",
        )
        self.assertEqual(
            callback_data(buttons),
            [
                b"hold:10",
                b"q_cancel:10",
                b"resume:11",
                b"q_cancel:11",
                b"cancel:20",
                b"q_pause",
                b"q_resume",
                b"queue:refresh",
            ],
        )

    def test_proxy_views_render_only_redacted_labels(self) -> None:
        state = ProxyViewState(
            current=0,
            auto=True,
            labels=("http://***@proxy.example:8080", "http://proxy2.example:8081"),
            current_label="http://***@proxy.example:8080",
        )

        main_text, main_buttons = proxy_view(state)
        list_text, list_buttons = proxy_list_view(state)

        self.assertIn("当前连接：代理 #1：http://***@proxy.example:8080", main_text)
        self.assertIn("✅ 代理 #1：http://***@proxy.example:8080", list_text)
        self.assertNotIn("password", main_text + list_text)
        self.assertEqual(
            callback_data(main_buttons),
            [b"proxy:add", b"proxy:auto", b"proxy:list", b"proxy:direct"],
        )
        self.assertEqual(
            callback_data(list_buttons),
            [
                b"proxy:back",
                b"proxy:use:0",
                b"proxy:test:0",
                b"proxy:del:0",
                b"proxy:use:1",
                b"proxy:test:1",
                b"proxy:del:1",
            ],
        )

    def test_webdav_views_render_from_config_state(self) -> None:
        state = WebDavConfigViewState(
            enabled=True,
            url="https://dav.example",
            user="backup-user",
            has_password=True,
            path="/backup",
            retry=5,
        )

        main_text, main_buttons = webdav_cfg_view(state)
        fields_text, fields_buttons = webdav_cfg_fields_view(state)

        self.assertIn("状态    ✅ 已启用", main_text)
        self.assertIn("🔑 密码    ***", main_text)
        self.assertIn("🔄 重试    5 次", fields_text)
        self.assertEqual(
            callback_data(main_buttons),
            [b"wd_cfg:off", b"wd_cfg:edit"],
        )
        self.assertEqual(
            callback_data(fields_buttons),
            [
                b"wd_cfg:url",
                b"wd_cfg:user",
                b"wd_cfg:pass",
                b"wd_cfg:path",
                b"wd_cfg:retry",
                b"wd_cfg:back",
            ],
        )

    def test_common_mode_and_session_keyboards_keep_callbacks(self) -> None:
        self.assertEqual(
            MODE_NAMES,
            {
                "ask": "每次询问",
                "always_spoiler": "总是雪花遮挡",
                "always_normal": "总是正常",
            },
        )
        self.assertEqual(
            callback_data(mode_buttons()),
            [b"mode:ask", b"mode:always_spoiler", b"mode:always_normal"],
        )
        keyboard = reply_keyboard()
        self.assertEqual(
            [button.text for button in keyboard.rows[0].buttons],
            [SESSION_BTN_BEGIN, SESSION_BTN_END],
        )

    def test_empty_session_is_still_visible_in_queue_view(self) -> None:
        text, _ = queue_view(QueueViewState(show_progress=True, has_sessions=True))
        self.assertIn("合集会话进行中：0 个媒体 · 0 条评论已收录", text)


if __name__ == "__main__":
    unittest.main()
