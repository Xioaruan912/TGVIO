from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PlayerContextFeedSourceTests(unittest.TestCase):
    def test_group_action_and_favorites_enter_swipe_contexts(self) -> None:
        ui = (ROOT / "player/web/src/ui.ts").read_text(encoding="utf-8")
        main = (ROOT / "player/web/src/main.ts").read_text(encoding="utf-8")
        self.assertIn('"查看同组视频"', ui)
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


if __name__ == "__main__":
    unittest.main()
