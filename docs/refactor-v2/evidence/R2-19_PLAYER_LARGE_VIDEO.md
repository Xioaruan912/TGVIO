# R2-19 Player Large-Video Category, Gestures and Range Cache

Release date: 2026-09-22

## Scope

Adds Bilibili-style playback to the Player and splits the library into short
(default) and large videos:

- **Categories** — clips longer than `TGVIO_PLAYER_LARGE_VIDEO_SECONDS`
  (default 300s) are "large". The short Rеels feed only decks short clips; a new
  "长视频" tab lists large clips.
- **Dedicated large-video player** — full-screen player with a buffered
  progress bar.
- **Gestures (both feed and large player)** — long-press fast-forward and
  horizontal drag-to-seek with a destination frame preview.
- **Play-while-caching** — a bounded on-disk byte-range cache with look-ahead
  prefetch so seeking in already-played regions is instant.
- **Settings → 播放设置** — toggles for all of the above (persisted locally).

## Server

- `application/range_cache.py` + `infrastructure/range_store.py`: chunked
  (default 1 MB), byte-bounded (default 8 GB), LRU disk cache. A miss reads
  exactly one chunk from upstream, never the whole file; `prime()` validates the
  first chunk before a response is prepared; prefetch is low priority and pauses
  while playback is active.
- `adapters/http/server.py`: `_stream` serves through the cache; a
  `?cache=1`/`?prefetch=1` query enables prefetch (added to `stream_url` by the
  feed/videos responses when the client asks). New `GET /api/v1/videos?category=`
  endpoint; media DTOs now carry `mime_type`, `codec` and `category`.
- `application/feed.py` + `infrastructure/sqlite.py`: `list_video_ids()` with a
  duration filter; the short feed deck only includes short clips.
- `main.py`: new settings `LARGE_VIDEO_SECONDS`, `CACHE_BYTES`, `CACHE_CHUNK_MB`;
  wires the cache and the threshold.
- `docker-compose.player.yml` / `deploy/player.env.example`: new variables.

## Web

- `gestures.ts`: pointer-based long-press (fast-forward while held) and
  horizontal drag scrub; vertical movement is left to the scroller
  (`touch-action: pan-y`).
- `preview.ts`: hidden `<video>` + canvas bubble showing the destination frame;
  seeks are coalesced and the element stops buffering when the drag ends.
- `large.ts`: dedicated player (play/pause, buffered bar, favorite, sound).
- `long.ts`: large-video library page with infinite scroll.
- `settings.ts`: local preferences; `main.ts` adds the 播放设置 toggles and the
  长视频 bottom-nav tab, and attaches the feed gestures.
- `feed.ts`: tap is now handled uniformly by the gesture layer.

## Verification

Offline: `npm run build` passes; full repository suite **799 passed**
(was 788); `release_guard.py verify-tree` (327 files) and `architecture`
(171 files) pass; `git diff --check` clean. New tests cover the range cache
(chunking, dedupe, LRU, prefetch, HTTP cache reuse), categories and the
`/api/v1/videos` endpoint.

Production (`https://csdn.im`), authenticated mobile Chromium:
- `GET /api/v1/videos?category=long` returns the longest clips (2821s / 2740s /
  2405s); `category=short` excludes them; `cache=1` yields `stream_url?...cache=1`;
  unauthenticated access is `401`.
- Large-video list: 20 rows; opening one plays (`readyState=4`, duration 2821s).
- Long press -> `playbackRate=2`, release -> `1` (both feed and large player).
- Horizontal drag shows the preview bubble (label `21:44`) and seeks
  (+1302s on the large player, +6.8s on the feed).
- Buffered progress indicator advances; disk cache grows (67 MB / 70 chunks).
- Regression: initial load / 12 slow swipes / 25 rapid swipes = **0 / 0 / 0**
  `429`; no horizontal overflow; playback DOM `<video>` count stays 3 (the
  preview element is a separate hidden node).

Deployment: Player-only. New image `tgvio-player:r2-19-largevideo2` (revision
`e73d342`), healthy, `restarts=0`; Bot container `83e013cc...` unchanged;
`bot_container_unchanged=true`. Rollback image
`tgvio-player:rollback-r2-19-largevideo2`.

## Remaining

- Large-video list order is by duration; a date/recency option is a follow-up.
- HEVC clips remain undecodable on many Android/Chromium builds.
- The 8 GB cache is host-disk bounded; it can be tuned or disabled with
  `TGVIO_PLAYER_CACHE_BYTES`.
