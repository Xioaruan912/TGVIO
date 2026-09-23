# Contextual Swipe Feeds Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add date-archive group browsing and a paginated favorites swipe feed while preserving the home short-feed position and playback privacy state.

**Architecture:** Keep one `FeedView` and `VideoPool` playback implementation, and add an explicit context state for home, archive group, and favorites feeds. Derive archive groups from the parent date directory of active catalog packages; expose bounded APIs for group pages and cursor-paged favorites.

**Tech Stack:** Python 3.11, aiohttp, SQLite, TypeScript, Vite, existing player release scripts.

**Spec:** `docs/superpowers/specs/2026-09-23-contextual-swipe-feeds-design.md`

## Global Constraints

- Do not add a database migration; derive a group from the parent directory already stored in `catalog_packages.remote_path`.
- Group and favorite APIs require the existing authenticated session and return no remote paths.
- Group pages include every active video in the selected archive date and order results stably by media ID.
- Favorites pages sort by `created_at DESC, media_id ASC` and use keyset cursors so more than 200 items can be browsed.
- Preserve the home feed's loaded clips, active index, shuffle position, mute state, and privacy lock when switching feed contexts.
- Keep all existing user changes in the worktree. Do not release unrelated pre-existing files without an explicit scope decision.
- Do not add runtime or frontend dependencies.

## Review Focus

- A media hash present in several archive dates must expose each date membership and open the date selected by the owner.
- Several favorites sharing the same second must page without skipping or repeating IDs.
- A late request from a departed context must not append items into the active feed.
- A failed final-page request must remain retryable and must not trigger the group's auto-return behavior.
- Returning on iPhone must restore the same home page while keeping video muted and covered until the user unlocks playback.

---

### Task 1: Add archive group membership and pages to the backend

**Files:**
- Modify: `src/tgvio_player/application/ports.py`
- Modify: `src/tgvio_player/infrastructure/sqlite.py`
- Modify: `src/tgvio_player/adapters/http/server.py`
- Test: `tests/test_player_http.py`
- Test: `tests/test_player_backend.py`

**Interfaces:**
- `PlayerCatalogRepository.list_media_groups(media_id: str) -> list[tuple[str, str]]` returns `(group_id, label)` pairs for every active date directory containing the media.
- `PlayerCatalogRepository.list_group_video_ids(group_id: str, *, after_id: str | None, limit: int) -> list[str]` returns active, distinct video IDs in stable ascending ID order. It returns an empty list for a group that no longer exists.
- `group_id` is the unpadded URL-safe base64 encoding of one validated date-directory name. The repository resolves it only against date-directory names found in active catalog paths; request values are never treated as paths.
- `GET /api/v1/groups/{group_id}/videos?limit=20&cursor=<media-id>` returns `{"items": [...], "has_more": bool, "next_cursor": string|null, "group": {"id": string, "label": string}}`.
- Media DTOs include `groups: [{"id": string, "label": string}]`; neither DTOs nor errors include archive remote paths.

- [ ] **Step 1: Add failing repository and HTTP cases**

In `tests/test_player_http.py`, extend `VideoCategoryTests` with two package fixtures under `TGVIO/2026-09-22/1` and `TGVIO/2026-09-22/2`, and a third package under `TGVIO/2026-09-23/1`. Give the same media ID active locations under both dates. Assert the media DTO lists both date groups, and that reading either group's first page returns only videos in that date, includes both short and long videos, and deduplicates a hash found in sibling packages. Assert unauthenticated requests return 401, an unknown group returns 404, a malformed group or cursor returns 400, and response JSON contains no `remote_path`.

In `tests/test_player_backend.py`, add a repository test that deactivates one package and verifies that its group membership and media no longer appear while another active package in the same date remains visible.

- [ ] **Step 2: Run the focused tests and confirm the new behavior fails**

Run: `python -m unittest discover -s tests -p test_player_http.py`

Run: `python -m unittest discover -s tests -p test_player_backend.py`

Expected: the new group assertions fail because the repository and route do not yet expose archive groups.

- [ ] **Step 3: Implement catalog group queries and the authenticated route**

Add the two repository methods to `PlayerCatalogRepository` and implement them in `PlayerCatalogRepositorySQLite`. Resolve only a single base64url date-directory component against active `catalog_packages.remote_path` parent names. Query distinct active `media.media_id` rows joined to active locations and packages, filter to video rows, and use `media_id > after_id ORDER BY media_id LIMIT limit + 1` to determine `has_more`.

Register the route in `PlayerHttpServer.application()`. Validate `limit` against `_MAX_FEED_LIMIT`; decode and re-encode group IDs to reject non-canonical base64url values; authenticate before returning group contents. Attach each media item's group memberships in `_media_dto`, and schedule stream preparation only for returned group items using the existing `_schedule_head_prefetch` path.

- [ ] **Step 4: Re-run the focused tests**

Run: `python -m unittest discover -s tests -p test_player_http.py`

Run: `python -m unittest discover -s tests -p test_player_backend.py`

Expected: group membership, sibling-package merge, duplicate suppression, authentication, validation, and inactive-package assertions pass.

- [ ] **Step 5: Commit the backend group API**

```bash
git add src/tgvio_player/application/ports.py src/tgvio_player/infrastructure/sqlite.py src/tgvio_player/adapters/http/server.py tests/test_player_http.py tests/test_player_backend.py
git commit -m "feat(player): add archive group feed API"
```

### Task 2: Add cursor pagination for favorites

**Files:**
- Modify: `src/tgvio_player/application/ports.py`
- Modify: `src/tgvio_player/application/feed.py`
- Modify: `src/tgvio_player/infrastructure/sqlite.py`
- Modify: `src/tgvio_player/adapters/http/server.py`
- Test: `tests/test_player_http.py`
- Test: `tests/test_player_backend.py`

**Interfaces:**
- `PlayerCatalogRepository.list_favorite_page(token_digest: str, *, limit: int, before: tuple[int, str] | None) -> list[tuple[str, int]]` returns up to `limit + 1` active favorite rows ordered by `created_at DESC, media_id ASC`.
- `ShuffleDeckService.favorite_page(token_digest: str, *, limit: int, cursor: tuple[int, str] | None) -> list[tuple[str, int]]` delegates to the repository.
- `GET /api/v1/favorites?limit=20&cursor=<opaque>` returns the same paged envelope as group videos. The cursor encodes the last `(created_at, media_id)` pair and is strictly decoded and validated by the HTTP adapter.

- [ ] **Step 1: Add failing cursor and scale cases**

In `tests/test_player_http.py`, add two favorited videos in one timestamp tie, request a page with `limit=1`, then request the returned cursor. Assert both IDs appear once in the prescribed order, `has_more` and `next_cursor` change correctly, invalid cursors return 400, unauthenticated requests return 401, and DTOs still omit locations.

In `tests/test_player_backend.py`, create and favorite 205 distinct active videos, fetch consecutive five-item repository pages using the returned final tuple as the next `before` tuple, and assert all 205 IDs are returned exactly once.

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: `python -m unittest discover -s tests -p test_player_http.py`

Run: `python -m unittest discover -s tests -p test_player_backend.py`

Expected: the tests fail because favorites currently return a single unpaged list capped at 200 IDs.

- [ ] **Step 3: Implement deterministic keyset paging**

Add the repository method with the continuation predicate `created_at < before_created_at OR (created_at = before_created_at AND media_id > before_media_id)`, retain the active-video join, and use one extra row to calculate `has_more`. Add the application service method. Update `_favorites` to validate the bounded page size, decode only a canonical JSON/base64url cursor containing an integer timestamp and 64-character lowercase media ID, fetch `limit + 1`, and build the response envelope. Keep current favorite writes unchanged.

- [ ] **Step 4: Re-run the focused tests**

Run: `python -m unittest discover -s tests -p test_player_http.py`

Run: `python -m unittest discover -s tests -p test_player_backend.py`

Expected: the tie case, malformed cursor case, authentication, and all-205-items paging pass without duplicate or missing IDs.

- [ ] **Step 5: Commit the favorites API**

```bash
git add src/tgvio_player/application/ports.py src/tgvio_player/application/feed.py src/tgvio_player/infrastructure/sqlite.py src/tgvio_player/adapters/http/server.py tests/test_player_http.py tests/test_player_backend.py
git commit -m "feat(player): paginate favorite videos"
```

### Task 3: Add context-aware API and feed page reset

**Files:**
- Modify: `player/web/src/types.ts`
- Modify: `player/web/src/api.ts`
- Modify: `player/web/src/feed.ts`
- Create: `player/web/src/context-feed.ts`

**Interfaces:**
- `ArchiveGroup` is `{ id: string; label: string }`; `Clip.groups` is an array of `ArchiveGroup` values.
- `api.groupVideos(groupId, limit, cursor, signal)` returns `{ items: Clip[]; hasMore: boolean; nextCursor: string | null; group: ArchiveGroup }`.
- `api.favoritePage(limit, cursor, signal)` returns `{ items: Clip[]; hasMore: boolean; nextCursor: string | null }`.
- `FeedView.replaceClips(clips: Clip[])` releases old page DOM, resets its media index and scroll candidate, creates pages for the new context, and starts at index 0.
- `ContextFeed` owns one non-home feed's mode, group ID if present, clip IDs, next cursor, end state, and an `AbortController`/generation counter so stale requests cannot mutate a later context.

- [ ] **Step 1: Define the context feed shape and page-reset behavior**

Add `ArchiveGroup`, extend `MediaDto` and `Clip`, and add response types for both APIs. Add `FeedView.replaceClips` and ensure it resets `el.scrollTop`, `candidate`, `programmaticTarget`, `pages`, and `indexByMedia` before creating new clip pages. Add a terminal page API to `FeedView` that creates one final scroll-sized page with a supplied label, separate from video indexes. Add `ContextFeed` with `loadFirstPage`, `loadMore`, `hasMore`, `clips`, and `dispose`; it ignores results after `dispose` and deduplicates IDs before appending.

- [ ] **Step 2: Add API methods and compile the player frontend**

Implement `groupVideos` and `favoritePage` using `URLSearchParams`, the shared authenticated request helper, and an `AbortSignal`. Parse the paged response envelopes and map DTOs through `clipFromMedia`.

Run: `npm run build` from `player/web`.

Expected: TypeScript compilation and Vite build pass before the UI is wired to the new methods.

- [ ] **Step 3: Commit context feed primitives**

```bash
git add player/web/src/types.ts player/web/src/api.ts player/web/src/feed.ts player/web/src/context-feed.ts
git commit -m "feat(player): add paged feed contexts"
```

### Task 4: Wire group and favorites swipe experiences

**Files:**
- Create: `tests/test_player_web_source.py`
- Modify: `player/web/src/ui.ts`
- Modify: `player/web/src/icons.ts`
- Modify: `player/web/src/styles/overlay.css`
- Modify: `player/web/src/main.ts`
- Modify: `player/web/src/feed.ts`

**Interfaces:**
- `ShellHandlers` gains `onOpenGroup` and `onBackFromContext` callbacks.
- Home retains its existing `clips`, `activeIndex`, and `FeedView` data while a group or favorites context is active.
- The current mode (`home`, `group`, `favorites`) is the source of truth for active clips, candidate changes, `commitActive`, `goNext`, loading, playback errors, and warm-up scheduling.

- [ ] **Step 1: Add failing source-level product assertions**

Add tests in `tests/test_player_web_source.py` for the required user-visible entry points: the player UI contains an accessible “同组视频” action; favorites navigation enters a swipe context instead of rendering selectable file rows; the group-end state has the specified completion copy; a failed page has an explicit retry path and does not enter the completion path; unfavoriting the active favorite does not replace it before advance; and the source has abort/generation protection for old context requests. Keep these assertions tied to the named UI handlers and labels rather than internal CSS selectors.

- [ ] **Step 2: Run the source-level checks and confirm they fail**

Run: `python -m unittest discover -s tests -p test_player_web_source.py`

Expected: failures identify the missing group action, favorites swipe flow, group-end behavior, and context request guard.

- [ ] **Step 3: Add the group action and context return controls**

Add a compact “同组视频” action to the player rail and a context header/back action. Hide or disable the group action when the current clip has no group membership. If there is one membership, open it directly; if there are several, show a date-only chooser. Keep the chooser independent from the video file list.

- [ ] **Step 4: Connect the mode state to playback and navigation**

In `main.ts`, preserve the home active index and scroll offset before entering a context. Pause and release the old `VideoPool` assignments with `pool.sync([], { paused: true, muted })`, replace the feed's page set with the first context page, use the same pool and controls, and route `commitActive`, `goNext`, `applyActive`, `scheduleWarm`, `prepareAhead`, and the media-error retry logic through the active context's clip array. On returning home, restore the exact saved page without changing `muted` or `privacyUnlocked`. Unfavoriting the active favorite updates its icon immediately and defers removal until the owner advances; failure restores the icon and favorite set.

- [ ] **Step 5: Implement group end, favorite end, empty, and retry states**

After the owner swipes past the last group item and the last page confirms `hasMore=false`, append a one-screen group-end sentinel. Settling on that sentinel returns home and shows “这组视频已看完，已返回短视频”. Do not treat the last active item, its video end event, or a request error as completion. For favorites, show an empty state when the first page is empty and append a “收藏已刷完” end page after the last confirmed page; Home navigation returns to the saved home item. Expose retry for a failed initial or subsequent page request.

- [ ] **Step 6: Run source checks and build**

Run: `python -m unittest discover -s tests -p test_player_web_source.py`

Run: `npm run build` from `player/web`.

Expected: source assertions and the full frontend build pass.

- [ ] **Step 7: Commit the two feed experiences**

```bash
git add player/web/src/ui.ts player/web/src/icons.ts player/web/src/styles/overlay.css player/web/src/main.ts player/web/src/feed.ts tests/test_player_web_source.py
git commit -m "feat(player): browse groups and favorites as feeds"
```

### Task 5: Verify, build, and deploy the player release

**Files:**
- Review only: all changed player files and `git status`
- Use: `scripts/player_release.sh`, `scripts/player_deploy.sh`, and `tests/test_player_deployment_artifacts.py`

**Interfaces:**
- Release artifact includes the backend and frontend changes together.
- Production deployment targets only the existing `tgvio-player` service; it does not rebuild or restart the Telegram bot service.

- [ ] **Step 1: Review release scope before packaging**

Run `git status --short` and compare every pre-existing worktree modification with the user's requested, previously accepted Player work. Keep this feature's changes isolated in commits. If the current worktree contains unrelated work that would enter the Player release, stop before packaging and identify those paths for owner direction; do not silently include them.

- [ ] **Step 2: Run focused backend and deployment checks**

Run: `python -m unittest discover -s tests -p test_player_http.py`

Run: `python -m unittest discover -s tests -p test_player_backend.py`

Run: `python -m unittest discover -s tests -p test_player_deployment_artifacts.py`

Run: `npm run build` from `player/web`.

- [ ] **Step 3: Build and deploy using the Player-only release flow**

Build the Player image from the reviewed release tree with `scripts/player_release.sh`. Deploy the resulting image through `scripts/player_deploy.sh` and its protected Player environment file. Preserve the script's pre/post assertion that the Bot container is unchanged. Never put the access secret or WebDAV password in command arguments or output.

- [ ] **Step 4: Verify the running production release**

Open `https://csdn.im/` in the already authorized browser. Confirm the current build responds, the group button opens same-date content and returns to the saved home item after swiping beyond the final group item, and Favorites scrolls through multiple pages without selecting files. Confirm the blurred/muted state remains locked after each context switch and verify the Bot container is still running unchanged.

- [ ] **Step 5: Record release evidence**

Report the deployed release identity, focused verification results, production browser observations, and any behavior not verified. Do not report deployment complete if the service health or either new feed fails.
