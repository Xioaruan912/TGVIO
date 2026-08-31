import unittest

from src.views import (
    BackupAttemptDetailView,
    BackupAttemptListItemView,
    BackupAttemptPageView,
    BackupFileItemView,
    HomeViewState,
    JobCardView,
    MODE_NAMES,
    PendingQueueItemView,
    ProxyViewState,
    QueueItemView,
    QueueViewState,
    SESSION_BTN_BEGIN,
    SESSION_BTN_END,
    WebDavConfigViewState,
    backup_attempt_detail_view,
    backup_attempt_page_view,
    mode_buttons,
    home_view,
    job_card_view,
    proxy_list_view,
    proxy_view,
    queue_view,
    reply_keyboard,
    webdav_cfg_fields_view,
    webdav_probe_view,
    webdav_write_confirm_view,
    webdav_write_result_view,
    webdav_required_policy_confirm_view,
    webdav_delete_confirm_view,
    webdav_cfg_view,
)
from src.webdav import WebDavProbeResult, WebDavWriteProbeResult


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
                b"h:r",
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
            [b"proxy:add", b"proxy:auto", b"proxy:list", b"proxy:direct", b"h:r"],
        )
        self.assertEqual(
            callback_data(list_buttons),
            [
                b"proxy:back",
                b"h:r",
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
            [b"wd_cfg:off", b"wd_cfg:test", b"wd_cfg:logs", b"wd_cfg:policy", b"wd_cfg:edit", b"h:r"],
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
                b"h:r",
            ],
        )

    def test_durable_webdav_attempt_views_use_short_id_callbacks(self) -> None:
        text, buttons = backup_attempt_page_view(
            BackupAttemptPageView(
                page=0,
                pages=2,
                total=6,
                items=(
                    BackupAttemptListItemView(
                        attempt_id=12, legacy_seq=77, state="failed", remote_dir="archive/77",
                        total_files=2, succeeded_files=1, failed_files=1, total_bytes=8,
                        created_at=1_700_000_000, error_code="webdav_server",
                    ),
                ),
            )
        )
        self.assertIn("job #77", text)
        self.assertIn("失败 1", text)
        self.assertIn(b"wd:a:12:0", callback_data(buttons))
        self.assertTrue(all(len(data) <= 64 for data in callback_data(buttons)))

        detail, detail_buttons = backup_attempt_detail_view(
            BackupAttemptDetailView(
                attempt_id=12, legacy_seq=77, state="failed", remote_dir="archive/77",
                retry_count=1, next_retry_at=None, error_code="webdav_server",
                page=0, pages=1, total=1,
                files=(
                    BackupFileItemView(
                        file_id=44, remote_name="abc.mp4", size_bytes=8, state="failed",
                        bytes_done=0, error_code="webdav_server",
                    ),
                ),
            )
        )
        self.assertIn("Attempt #12", detail)
        self.assertIn(b"wd:fr:44", callback_data(detail_buttons))
        self.assertIn(b"wd:ar:12", callback_data(detail_buttons))
        self.assertIn(b"wd:del:12", callback_data(detail_buttons))

        policy_text, policy_buttons = webdav_required_policy_confirm_view(123456789)
        self.assertIn("already published", policy_text.lower().replace("已经发布", "already published"))
        self.assertEqual(
            callback_data(policy_buttons),
            [b"wd_bp:y:123456789", b"wd_bp:n:123456789", b"wd_cfg:back"],
        )

        delete_text, delete_buttons = webdav_delete_confirm_view(
            987654321, remote_dir="archive/77", file_count=2, total_bytes=8
        )
        self.assertIn("逐个删除数据库记录的具体文件", delete_text)
        self.assertEqual(
            callback_data(delete_buttons),
            [b"wd_dr:y:987654321", b"wd_dr:n:987654321", b"wd:p:0"],
        )
        probe_text, probe_buttons = webdav_probe_view(
            WebDavProbeResult(
                True,
                207,
                True,
                quota_used_bytes=1024,
                quota_available_bytes=2048,
                message="读取成功",
            )
        )
        self.assertIn("✅ 路径可读取", probe_text)
        self.assertIn("可用额度：2.0 KB", probe_text)
        self.assertIn("只执行只读 PROPFIND", probe_text)
        self.assertEqual(
            callback_data(probe_buttons),
            [b"wd_cfg:test", b"wd_cfg:wtest", b"wd_cfg:back", b"h:r"],
        )

        confirm_text, confirm_buttons = webdav_write_confirm_view(123456789)
        self.assertIn("只有确认后", confirm_text)
        self.assertEqual(
            callback_data(confirm_buttons),
            [b"wd_w:y:123456789", b"wd_w:n:123456789", b"wd_cfg:back"],
        )
        result_text, _ = webdav_write_result_view(
            WebDavWriteProbeResult(True, True, True, True, "全部成功")
        )
        self.assertIn("测试文件清理：成功", result_text)

    def test_home_console_and_task_card_use_short_callbacks(self) -> None:
        text, buttons = home_view(
            HomeViewState(
                session_active=True,
                session_media=8,
                session_texts=2,
                running=2,
                waiting=3,
                failed=1,
                webdav_enabled=True,
                webdav_health="正常",
                disk_used_gb=18.4,
                disk_total_gb=50.0,
            )
        )
        self.assertIn("Telegram 媒体中转站", text)
        self.assertIn("合集：8 个媒体 · 2 条文字", text)
        self.assertIn("队列：2 运行 · 3 等待 · 1 失败", text)
        self.assertIn("请选择一个操作", text)
        self.assertEqual(
            callback_data(buttons),
            [
                b"h:begin", b"h:end",
                b"h:q", b"h:f",
                b"h:w", b"h:s",
                b"h:status", b"h:help",
                b"h:r",
            ],
        )

        card, card_buttons = job_card_view(
            JobCardView(
                seq=28,
                phase="downloading",
                media_count=8,
                total_bytes=1524713390,
                pct=63,
                speed_bps=24.8 * 1024 * 1024,
                eta_seconds=18,
                item=5,
                items=8,
            )
        )
        self.assertIn("⬇️ 任务 #28 · 正在下载", card)
        self.assertIn("63%", card)
        self.assertIn("预计剩余 18 秒", card)
        self.assertTrue(all(len(data) <= 64 for data in callback_data(card_buttons)))

        ready, _ = job_card_view(
            JobCardView(
                seq=28,
                phase="ready",
                media_count=1,
                compat_note="🧩 已完成 faststart（无损 remux）",
            )
        )
        self.assertIn("已完成 faststart", ready)

        unknown, _ = job_card_view(
            JobCardView(
                seq=29,
                phase="downloading",
                transferred_bytes=64 * 1024 * 1024,
                pct=0,
                speed_bps=8 * 1024 * 1024,
            )
        )
        self.assertIn("已传输：64.0 MB", unknown)
        self.assertNotIn("0%", unknown)

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
