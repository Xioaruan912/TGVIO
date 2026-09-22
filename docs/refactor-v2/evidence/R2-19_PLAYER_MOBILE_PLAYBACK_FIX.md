# R2-19 Player Mobile Playback Fix

Release date: 2026-09-22

## Release identity

- Fix commits:
  - `755a6e1` (`fix(player): harden production mobile playback`)
  - `ae3db0b` (`fix(player): settle feed before loading to avoid rapid-swipe 429 storms`)
- Player image: `tgvio-player:r2-19-ae3db0b`
- Previous production image: `tgvio-player:r2-19-db3c45d`
- Rollback image: `tgvio-player:rollback-r2-19-ae3db0b`

## Root causes

1. **Stream capacity exhaustion (critical).** The server counted every stream
   request against a per-client cap of 4, and every visitor shared one identity
   because the Player ignored the reverse proxy's `X-Forwarded-For`. A single
   phone loading the feed already issued roughly 3 `<video>` requests plus
   speculative warm-up ranges, so the active video's own request was rejected
   with `429`. The `<video>` then reported a media error, which the client
   permanently blacklisted as "unplayable", producing a skip storm.
   Reproduced on production: 24 `429` during a single load, 21 during 25 rapid
   swipes, current video ending in `MEDIA_ERR_SRC_NOT_SUPPORTED`.
2. **Transient errors treated as permanent.** `pool.onError` added the clip to a
   session-long blacklist with no distinction between network/`429`/`5xx` and a
   genuine decode failure.
3. **Hash-named static assets served `Cache-Control: no-store`**, so the JS/CSS
   bundle was re-downloaded on every load.
4. **Video viewport was not full height.** `.stage` reserved `56px + safe-area`
   for the bottom navigation, so the video was 788px on an 844px phone instead
   of a true full-screen Reels window.
5. **Login input forced a numeric keyboard**, making a 32+ character
   alphanumeric access secret impossible to type on mobile.
6. **Slow archive upstream** (roughly 3-8s per 1 MiB range) meant warm-up
   fetches held slots for seconds; warming N+1..N+3 both wasted capacity and did
   not help the open-ended `bytes=0-` request the browser actually sends.

## Changes

- `src/tgvio_player/adapters/http/server.py`
  - `resolve_client()` trusts `X-Real-IP` / right-most `X-Forwarded-For` only
    when the immediate peer is a private/loopback proxy; used for login
    throttling and stream accounting.
  - Speculative preloads (`X-TGVIO-Preload: 1`) no longer count against the
    playback budget, are capped at a small share of global slots, and are the
    first requests dropped under pressure.
  - `Cache-Control`: `no-store` for `/api/*` and `/healthz`, `no-cache` for the
    HTML entry, `public, max-age=31536000, immutable` for `/assets/*`.
  - Range responses fall back to the catalog `mime_type` when the upstream
    returns `application/octet-stream`.
- `player/web/src/api.ts`: preload tag header and a cheap `probe()` used to
  classify media errors.
- `player/web/src/preload.ts`: warm only N+1 with a light range.
- `player/web/src/player.ts`: per-assignment load token / abort signal so a
  late `loadeddata`/`error` from an old source cannot mark the new page ready or
  mis-attribute an error; `retryCurrent()`.
- `player/web/src/main.ts`: device dedup (feed + favorites + library share one
  `seenIds` set), deferred warm only after the active video is playing,
  transient-vs-permanent media-error handling with bounded retries, and a
  `visualViewport`/resize re-snap.
- `player/web/src/feed.ts`: settle debounce 110ms -> 200ms so rapid flings load
  once after movement stops instead of on every intermediate frame.
- `player/web/src/ui.ts`: login input accepts alphanumeric secrets.
- `player/web/src/styles/shell.css`, `styles/overlay.css`: video fills the full
  dynamic viewport; bottom navigation is a real overlay; clip info and the
  action rail clear the navigation and safe area on mobile only.

## Verification

- Offline: Player tests **45 passed** (was 42); full repository **777 passed**
  (was 774); `release_guard.py verify-tree` and `architecture` pass
  (313 / 167 files); `git diff --check` clean.
- Production `https://csdn.im`, real API / real media / real WebDAV, authenticated
  Chromium with mobile emulation:
  - `429` responses across initial load, 12 slow swipes and 25 rapid swipes:
    **0 / 0 / 0** at 390x844, 393x852, 430x932, 1440x900 and 1920x1080.
  - Real playback confirmed: `readyState=4`, `currentTime` advancing,
    `duration=20.3s`.
  - `video` elements <= 3 and playing videos <= 1 at every viewport.
  - No horizontal overflow (`scrollWidth == innerWidth`).
  - Mobile video viewport equals the full viewport height (844/852/932).
  - Interactions: favorite, pause/resume, seek (issues a byte Range), sound
    toggle, settings sheet and favorites sheet all keep the current video
    element and playback position.
  - HTTP: root `200`, healthz `200`, unauthenticated feed/favorites `401`,
    login `200`, feed `200`, media `200`, range `206`; `/assets/*` immutable,
    `/healthz` no-store, `/` no-cache.
- Deployment isolation: Bot container `83e013cc...` unchanged, `restarts=0`;
  Player recreated healthy with `restarts=0`; `bot_container_unchanged=true`.

## Known content limitation

The catalog contains 886 videos: 780 `video/mp4` and 106 `video/quicktime`;
by codec, 744 `h264` and 142 `hevc`. HEVC clips are not decodable by many
Android/Chromium builds. They are now handled as a genuine (post-probe)
undecodable clip and skipped, instead of being blamed on the network. Filtering
browser-incompatible codecs from the feed is left as a follow-up.
