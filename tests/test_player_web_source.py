from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PlayerContextFeedSourceTests(unittest.TestCase):
    def test_folder_action_and_favorites_have_separate_owners(self) -> None:
        ui = (ROOT / "player/web/src/ui.ts").read_text(encoding="utf-8")
        main = (ROOT / "player/web/src/main.ts").read_text(encoding="utf-8")
        actions = (ROOT / "player/web/src/components/media-actions.ts").read_text(encoding="utf-8")
        self.assertIn("buildMediaActions(handlers)", ui)
        self.assertIn('"浏览所在文件夹"', actions)
        self.assertIn('onOpenGroup: openCurrentFolder', main)
        self.assertIn('void enterContext("favorites")', main)
        self.assertNotIn("async function openFavorites", main)

    def test_selected_playback_never_inserts_an_entire_group_into_home(self) -> None:
        main = (ROOT / "player/web/src/main.ts").read_text(encoding="utf-8")
        self.assertNotIn("function openGroupList", main)
        self.assertNotIn("feedView.insertAfter", main)
        self.assertIn("new LibraryPlayback", main)
        self.assertIn("加载失败，点击重试", main)
        # One shared owner hands playback back to whichever grid started it.
        self.assertIn("const playback = createCollectionPlayback(active => page?.setPlaybackActive(active));", main)
        self.assertIn("if (wasPlaying) syncPage(false);", main)

    def test_favorites_browse_uses_the_cover_grid_and_an_explicit_player(self) -> None:
        main = (ROOT / "player/web/src/main.ts").read_text(encoding="utf-8")
        favorites = (ROOT / "player/web/src/favorites.ts").read_text(encoding="utf-8")
        self.assertIn("new FavoritesPage(", main)
        self.assertIn("openFavorites();", main)
        self.assertIn("buildCoverTile", favorites)
        # Browsing favorites never builds a player; playback stays explicit.
        self.assertNotIn("createElement(\"video\")", favorites)
        self.assertIn("播放已加载 (", favorites)

    def test_context_requests_abort_and_deduplicate_old_pages(self) -> None:
        context = (ROOT / "player/web/src/context-feed.ts").read_text(encoding="utf-8")
        self.assertIn("this.controller.abort()", context)
        self.assertIn("requestGeneration !== this.generation", context)
        self.assertIn("if (!known.has(clip.id))", context)

    def test_playback_owners_never_lose_or_resurrect_control(self) -> None:
        main = (ROOT / "player/web/src/main.ts").read_text(encoding="utf-8")
        large = (ROOT / "player/web/src/large.ts").read_text(encoding="utf-8")
        playback = (ROOT / "player/web/src/library-playback.ts").read_text(encoding="utf-8")
        # A late feed retry must respect the current library/large-player owner.
        self.assertIn("const feedOwnsPlayback = (): boolean => !libraryPage && !favoritesPage && !longVideosOpen && !largePlayer;", main)
        self.assertEqual(main.count("!feedOwnsPlayback()"), 2)
        # A destroyed player must not resurrect its source after an awaited delete.
        self.assertIn("if (this.destroyed) return;", large)
        # Only the live playlist player may return to selection after a delete.
        self.assertIn("if (this.closed || this.player !== player) return;", playback)


class PlayerLibraryIdleDeadlineSourceTests(unittest.TestCase):
    """Automatic playlist advances must not restart the 60s privacy minute."""

    def test_players_share_one_real_deadline(self) -> None:
        idle = (ROOT / "player/web/src/idle-privacy.ts").read_text(encoding="utf-8")
        large = (ROOT / "player/web/src/large.ts").read_text(encoding="utf-8")
        playback = (ROOT / "player/web/src/library-playback.ts").read_text(encoding="utf-8")
        self.assertIn("export class IdleActivityWindow", idle)
        self.assertIn("resetActivityOnEnable", idle)
        self.assertIn("activityWindow: options.idleWindow", large)
        self.assertIn("resetActivityOnEnable: options.idleResetOnEnable", large)
        self.assertIn("idleWindow: this.idleWindow", playback)
        self.assertIn("idleResetOnEnable: false", playback)
        self.assertEqual(playback.count("this.idleWindow.touch()"), 2)
        self.assertIn("this.idlePrivacy.activity()", large)


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


class PlayerCoverTileSourceTests(unittest.TestCase):
    """Browsing stays metadata-only, covers stay honest and select stays explicit."""

    def test_one_cover_unit_is_shared_by_every_browse_surface(self) -> None:
        tile = (ROOT / "player/web/src/components/cover-tile.ts").read_text(encoding="utf-8")
        self.assertIn('export type CoverState = "loading" | "ready" | "missing" | "failed";', tile)
        # An unknown duration is never rendered as 0:00.
        self.assertIn('return "时长未知";', tile)
        # Selection is its own control: a button must never nest another button.
        self.assertIn('element("button", "cover-tile-select")', tile)
        self.assertIn('element("button", "cover-tile-preview")', tile)
        for module in ("library.ts", "favorites.ts", "long.ts"):
            source = (ROOT / "player/web/src" / module).read_text(encoding="utf-8")
            self.assertIn("buildCoverTile", source, f"{module} must reuse the shared cover unit")

    def test_library_grid_starts_no_media_and_gates_selection_behind_a_mode(self) -> None:
        library = (ROOT / "player/web/src/library.ts").read_text(encoding="utf-8")
        self.assertIn('this.list.classList.add("cover-grid")', library)
        self.assertIn("setSelectMode", library)
        self.assertIn('this.selectionBar.hidden = !this.selectMode;', library)
        # The on-demand preview is the only video the list may create.
        self.assertEqual(library.count('createElement("video")'), 1)
        self.assertIn('video.preload = "none"', library)


if __name__ == "__main__":
    unittest.main()
