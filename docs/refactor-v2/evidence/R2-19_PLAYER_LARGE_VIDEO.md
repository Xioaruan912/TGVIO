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

## Follow-up: network download rate HUD

The video DTO now carries `size_bytes`, and a `NetworkMeter` (web) estimates the
download rate from how fast the media buffer grows (`buffered` seconds ×
size/duration), smoothed with an EMA and sampled every 500 ms. The rate is shown
as `↓ KB/s` / `↓ MB/s` in the top-right of the short-video feed and of the
large-video player. A "显示网速" toggle in 播放设置 (default on) controls it.
Verified on production: the large player showed a steady ~530 KB/s while
streaming the 47-minute clip, and the feed showed a 3.0 MB/s spike as a fresh
clip buffered.

## Follow-up: seamless short-feed switching, tap fix and fullscreen

- **Seamless switching.** The disk range cache no longer pauses read-ahead just
  because a stream is active (it only pauses when every stream slot is busy),
  and `/api/v1/feed` / `/api/v1/videos` warm the head of the first three clips
  (whole file when <= 4 MB). The client warms N+1..N+3 and normal scrolling no
  longer cancels the warm. Measured on production with ~2.5 s per clip:
  switching latency median **0.3 s** (previously alternating 0.3 s / 4-5 s).
- **Single centre control.** A tap while autoplay is blocked now starts
  playback instead of toggling pause, and the pause indicator is cleared on
  every clip change, so the white play affordance and the dark pause indicator
  no longer appear together.
- **Fullscreen.** A fullscreen button was added to the short-feed action rail and
  to the large-video controls (`requestFullscreen` with an iOS
  `webkitEnterFullscreen` fallback). Large-player gestures were moved onto the
  video stage so the control bar keeps receiving clicks.
  Verified on production: both enter fullscreen.
- Regression: load / 12 slow swipes / 25 rapid swipes = 0 / 0 / 0 `429`.
  Deployment `tgvio-player:r2-19-seamless2` (revision `35f2544`), healthy,
  Bot unchanged.
