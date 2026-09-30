from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PlayerContextFeedSourceTests(unittest.TestCase):
    def test_group_action_and_favorites_enter_swipe_contexts(self) -> None:
        ui = (ROOT / "player/web/src/ui.ts").read_text(encoding="utf-8")
        main = (ROOT / "player/web/src/main.ts").read_text(encoding="utf-8")
        actions = (ROOT / "player/web/src/components/media-actions.ts").read_text(encoding="utf-8")
        self.assertIn("buildMediaActions(handlers)", ui)
        self.assertIn('"查看同组视频"', actions)
        self.assertIn('onOpenGroup: openGroupChooser', main)
        self.assertIn('void enterContext("favorites")', main)
        self.assertNotIn("async function openFavorites", main)

    def test_group_completion_and_retry_are_separate_states(self) -> None:
        main = (ROOT / "player/web/src/main.ts").read_text(encoding="utf-8")
        self.assertIn("这组视频已看完，已返回短视频", main)
        self.assertIn("加载失败，点击重试", main)
        self.assertIn("current.hasMore && !current.error", main)

    def test_context_requests_abort_and_deduplicate_old_pages(self) -> None:
        context = (ROOT / "player/web/src/context-feed.ts").read_text(encoding="utf-8")
        self.assertIn("this.controller.abort()", context)
        self.assertIn("requestGeneration !== this.generation", context)
        self.assertIn("if (!known.has(clip.id))", context)


class PlayerDownloadAndTailSourceTests(unittest.TestCase):
    """Guard the client wiring for the download action and the tail warm-up."""

    def test_download_action_is_wired_to_the_attachment_stream(self) -> None:
        main = (ROOT / "player/web/src/main.ts").read_text(encoding="utf-8")
        ui = (ROOT / "player/web/src/ui.ts").read_text(encoding="utf-8")
        icons = (ROOT / "player/web/src/icons.ts").read_text(encoding="utf-8")
        actions = (ROOT / "player/web/src/components/media-actions.ts").read_text(encoding="utf-8")
        self.assertIn("buildMediaActions(handlers)", ui)
        self.assertIn('"下载原片"', actions)
        self.assertIn("onDownload: () => void;", ui)
        self.assertIn("downloadBtn.addEventListener(\"click\", handlers.onDownload)", actions)
        self.assertIn("onDownload: downloadCurrent", main)
        self.assertIn('`${clip.streamUrl}${separator}download=1`', main)
        self.assertIn('| "download"', icons)

    def test_seeking_towards_the_end_asks_the_server_to_warm_the_tail(self) -> None:
        main = (ROOT / "player/web/src/main.ts").read_text(encoding="utf-8")
        api = (ROOT / "player/web/src/api.ts").read_text(encoding="utf-8")
        self.assertIn("warmTail(clip.id);", main)
        self.assertIn("time >= video.duration * 0.7", main)
        self.assertIn("async prepareTail(mediaId: string)", api)
        self.assertIn("/prepare?tail=1", api)


if __name__ == "__main__":
    unittest.main()
