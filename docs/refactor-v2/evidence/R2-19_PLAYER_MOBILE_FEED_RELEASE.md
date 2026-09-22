# R2-19C/D Player Mobile Feed Release

Release date: 2026-09-22

## Release identity

- Frontend rebuild + backend fixes: `7a45d71d060cebb7ddf9d787ea8a8756f1ac654c`
  (`feat(player): ship tiktok-style responsive video feed`)
- Mobile settings entry: `ed3655b2d809f6d984e56a740955f024d1f942d2`
- Stream budget for the 3-video window: `40fb8315f3de349e836e54bd0d83c69fcb891877`
- Faster first frame (N+1 startup warm): `9fc3a625cf09879a60a49af71529ba4ef043c233`
- Skip unplayable clips: `95b86589d886f579fad1dac9257444fdee100688`
- Production Player image: `tgvio-player:r2-19-95b8658`
- Production release source: `/root/tgvio-player/releases/r2-19-95b8658/source`

## Root causes fixed

1. **Whole-page re-render destroyed the playing video.** The old frontend called
   `render()` on favorite, pause, waiting/stalled, and every active-index change,
   rebuilding the 3 `<video>` elements from `innerHTML`. This caused reloads,
   black flashes and repeated network fetches on the phone.
2. **Favorites were broken in production.** `PUT/DELETE /api/v1/media/:id/favorite`
   compared the browser `Origin` (`https://csdn.im`) against `request.scheme://request.host`,
   but aiohttp sees `http://csdn.im` behind the TLS-terminating nginx, so every
   favorite mutation returned `403`.
3. **Stream 429s killed playback.** `TGVIO_PLAYER_MAX_STREAMS_PER_CLIENT=2` could
   not cover `current + next metadata + startup warm`; a `429` on a media range is
   fatal to the `<video>` element.
4. **Dashboard-style UI on the main page.** Archive Sync / Playback Engine / Up Next
   cards dominated the phone screen instead of the video.

## Frontend architecture (player/web/src)

```text
main.ts       bootstrap-once orchestrator; no whole-app render after boot
api.ts        feed/favorites/favorite/login/logout + startup warm
types.ts      Clip/MediaDto/PreloadLevel
feed.ts       vertical scroll-snap pages (metadata window, light posters)
player.ts     VideoPool: exactly 3 long-lived <video> elements moved between pages
preload.ts    PreloadCoordinator: N+1..N+3 warm, aborts on current pressure
ui.ts         shell/login/sheets built once, then局部 updates only
styles/       base/shell/feed/overlay/sheet
```

- Exactly **3** `<video>` elements exist for the whole session; they are reused and
  moved into the previous/current/next page hosts. A clip change never creates a
  new DOM tree.
- `waiting/stalled` only sets PreloadCoordinator pressure and pauses background
  warm-ups; the current `<video>` DOM is never rebuilt.
- Favorite/pause/resume/progress/mute update only the affected DOM nodes.
- `scroll-snap-type: y mandatory` + `scroll-snap-stop: always` drives real swipes;
  the active index commits only after the snap settles.
- `requestVideoFrameCallback()` (fallback `loadeddata`/`playing`) fades the poster
  out after the first decoded frame, so switching does not flash black/white.
- Random stays the persistent server Shuffle Deck; favorites never change odds.
- Debug overlay only with `?debug=1` and never prints paths or credentials.

## Backend fixes (src/tgvio_player)

- `_require_same_origin()` now compares the `Origin` host against `Host` /
  `X-Forwarded-Host`, scheme-agnostic, so reverse-proxy TLS termination no longer
  breaks same-origin mutations. Cross-site origins are still rejected.
- Added read-only `GET /api/v1/favorites` (session-scoped, minimal DTO) so the
  mobile Favorites tab can list saved clips.
- `docker-compose.player.yml` / `deploy/player.env.example` default stream budget
  is now 8 global / 4 per client, matching the 3-video + warm window.

## Verification

### Automated

- Player tests: 42 passed (`tests.test_player_http`, `test_player_catalog`,
  `test_player_backend`, `test_player_runtime`, `test_player_deployment_artifacts`).
- Full repository suite: **774 tests passed**.
- `release_guard.py verify-tree .`: passed (376 files).
- `release_guard.py architecture .`: passed (167 Python files, max 1000 lines).
- `git diff --check`: clean. Frontend `npm run build` (tsc + vite): clean.

### Production API (https://csdn.im)

- root `200`, healthz `200`, unauthenticated feed `401`.
- login `200`; feed `200`; media `200`; Range `bytes=0-1048575` -> `206` 1 MiB.
- Favorite `PUT` and `DELETE` with `Origin: https://csdn.im` -> `200`;
  `GET /api/v1/favorites` -> `200`; hostile Origin -> `403`.

### Real browser (Google Chrome, codec-capable)

- Mobile 390x844 / 393x852 / 430x932 and desktop 1440x900 / 1920x1080:
  `scrollWidth == clientWidth` (no horizontal overflow), real video playing
  (`readyState 4`, buffered ranges, `currentTime` advancing), DOM video elements
  `<= 3`, at most one playing, zero page errors.
- Favorite: same `<video>` element, `currentTime` not reset, button toggles.
- Pause/resume: same element, time continues, no reload.
- Seek: `currentTime` jumps to target and multiple `206` Range responses observed.
- Swipe: title changes, exactly 3 video elements, one playing.
- Rapid 8-page swipe: settles on the final clip, `<= 3` video elements, no
  simultaneous audio; warm window re-plans around the new active index.
- Swipe-to-first-frame after warm-up: typically ~0.2-2 s, with slow/large or
  non-browser-playable archived files occasionally taking longer (poster covers
  the wait and an unplayable clip is skipped automatically).
- UI: Favorites sheet lists the favorited clip; Library lists the session clips;
  Settings opens from the mobile top-bar gear and toggles sound; Random advances
  the deck.

## Residual follow-up

- Some archived `.mov` files carry codecs/audio that a given browser cannot decode
  or are very large; those clips can still take a few seconds to first frame or be
  skipped. This is content/network-bound, not a Player render bug. A future
  derivative/remux stage (design §10A.12) can normalize them.
- Favorites remain session-scoped (Player session digest). They survive refresh
  but not logout/re-login; owner-scoped persistence is a future migration.

## Rollback

- Previous image retained: `tgvio-player:r2-19-pin-710ad08` and
  `tgvio-player:rollback-r2-19-pin-710ad08`.
- Player env backups: `player.env.bak-r2-19-40fb831`, `.bak-r2-19-9fc3a62`,
  `.bak-r2-19-95b8658`, `.bak-limits-r2-19-7a45d71`.
- Rollback command (Player only, never touches the Bot):
  `scripts/player_rollback.sh --env-file /root/tgvio-player/player.env --image tgvio-player:r2-19-pin-710ad08 --execute`
- The Bot container `tgvio` ID was verified unchanged across every Player
  cutover (`bot_container_unchanged=true`).
