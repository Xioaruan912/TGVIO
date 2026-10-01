# Player Web 开发入口

统一验证：`npm run check`；浏览器：`npm run test:browser`。
Node 20.19+ / 22.12+；严格 TS 测试编译到临时 .test-dist，退出自动清理。
完整说明见 [当前开发文档](../../docs/development/README.md) 与 [Player 约定](../AGENTS.md)。
当前视觉与交互边界见 [Player 前端设计](../../docs/development/PLAYER_FRONTEND.md)。

## 既有 Player Web

R2-19C Vite/TypeScript frontend. In normal builds it uses the authenticated,
same-origin Player API only:

- `GET /api/v1/feed`
- `GET /api/v1/media/:id`
- `PUT` / `DELETE /api/v1/media/:id/favorite`

The browser receives opaque media IDs and relative stream URLs only. It never
handles WebDAV URLs, filesystem paths, credentials, or access secrets.

Run locally after installing frontend dependencies:

```sh
npm install
npm run dev
```

The API uses the authenticated Player session cookie. Sign in through the Player
login endpoint before opening the feed.

On a new installation, open Player settings and configure the writable WebDAV
destination for favorites. Until then the settings page shows “尚未配置收藏存储”;
favorites remain in Player SQLite and failed backups can be retried after setup.
The `.invalid` example endpoint is intentionally unreachable.

For isolated visual development only, start Vite with an explicit mock flag:

```sh
VITE_PLAYER_MOCK=true npm run dev
```

Without that exact flag, failed API requests show an error state; they never fall
back to mock media. There are always exactly three `<video>` elements: previous,
current, and next. Metadata lookahead and startup-range warm-up stay bounded by
the existing preload coordinator.

### Playback diagnostics

Each home-feed batch gets an opaque playback session ID. Video stream requests,
preload requests, and frontend playback events carry that ID so one playback
can be separated from older sessions. To inspect only recent Player events on
the VPS, start with a short time window:

```sh
docker logs --since 15m tgvio-player 2>&1 | grep 'player_event'
```

After finding a `session` value in a `frontend_playback` event, filter the
timeline to that session:

```sh
docker logs --since 15m tgvio-player 2>&1 | grep 'player_event' | grep '"session":"<session-id>"'
```

`http_request` events distinguish `video` streams from `preload` traffic and
classify outcomes such as `not_found`, `capacity_limited`, `server_error`,
`client_disconnected`, and `stream_ok`. `frontend_playback` events report
confirmed playback, media errors, probe status, retries, and skipped clips.
The media value is a stable fingerprint rather than the raw catalog ID.

### Local regression checks

```sh
npm test
npm run build
# Requires an already installed Google Chrome (or CHROME_BIN).
npm run test:browser
```

The browser check starts a temporary loopback-only Vite fixture and an isolated,
temporary Chrome profile; both are closed afterwards. It never uses production
login state, real WebDAV, or Bot configuration. It verifies computed styles,
hit-testing, control focus isolation and long-player source-change intent at
390/430/768/1440 widths. Media events in the fixture are simulated: this is not
an Android/iOS touch or real Range-stream playback acceptance test.

Playback loading has one status layer; the poster is decorative and does not
cover an existing frame during buffering. Feed refill is bounded, favorite
mutations share one per-media queue across views, and cancelled gestures release
seek/fast-forward state. See `../AGENTS.md` for the development contract.

### Idle privacy lock

Short Feed automatically hides, pauses and mutes after 60 seconds without user
interaction, even while playing. Long videos are exempt during actual playback;
paused/ended/loading/buffering states lock after a full idle minute. Pointer,
touch-drag, wheel, keyboard and input activity reset the deadline; media progress,
automatic snap and preload do not. Explicit playback unlocks the picture, but
sound remains muted until enabled again. This is a web privacy lock, not an OS
screen lock. Existing background privacy rules remain in force.

### SKY mobile-first UI

The header, media stage, persistent playback panel and navigation are separate
regions. Short/long progress controls share a 48px pointer-captured seek binder:
dragging previews, release commits once, cancel/background/source change does not
seek. Time updates cannot replace a drag preview. Secondary screens have one
lifecycle owner and switching tabs tears down the previous view.

Light sky tokens, cards, grouped settings, accessible modal focus/Escape/backdrop
handling and reduced-motion-aware animations use native TypeScript/CSS only.
Privacy lock uses an opaque cover (not a blurred exposed frame); it does not
rebuild video nodes or change normal buffering presentation.

### Cover grid (library / favorites / long videos)

`src/components/cover-tile.ts` is the single cover unit for every browse surface.
The picture fills the media box, only a bounded bottom scrim carries white text,
and the type label is shown only in a mixed short/long grid. Cover states are
explicit and never faked:

- `loading` — a same-size neutral placeholder while the lazy `<img>` resolves.
- `missing` — the API returned no cover; a neutral placeholder with “暂无封面”.
- `failed` — two image errors or a 20-second request deadline; “封面加载失败” plus a bounded retry. A broken
  cover never claims the video itself is unplayable.
- `ready` — the decoded picture.

Browsing is metadata-only: the lists create zero `<video>` elements, one preview
video at most runs at a time, and a preview is always an explicit action. The
library video stage is a two-column cover grid on phones (container-aware
180/200px minima on wider content areas); long videos use the 16:9 `cover-grid-wide` variant.

Image requests enter one shared two-slot queue only near the viewport. Leaving
the viewport cancels waiting jobs; page disposal cancels active jobs, observers
and deadlines. A single automatic retry is allowed; manual retry never starts
playback. Title and metadata occupy separate overlay rows, and failed-image
retry sits away from title/duration. Call CoverTileHandle.destroy() whenever a
page replaces or removes its cards.

The privacy lock is directly reachable alongside play, mute and favorite.
Low-height landscape splits media and controls into columns; lists retain their
own scroll area. Quality/device settings expose supported capabilities only.

Multi-select is an explicit mode entered from the toolbar (“选择” / “退出多选”).
While browsing, tapping a cover plays that clip; in select mode the same tap
toggles selection and never starts playback. The selection bar reports the real
count (`播放选中 (n/100)`), selection survives paging and category filters, and
leaving a folder clears it with an explicit notice. Playback of a single cover or
of the selection runs through one `LibraryPlayback` queue (owned by
`createCollectionPlayback` in `main.ts`) so the library and favorites grids share
the same idle/privacy contract.

The favorites page (`src/favorites.ts`) is a metadata-only grid over
`GET /api/v1/favorites`; its scope label says `播放已加载 (n)` because only the
loaded page is queued, and it keeps local favorite truth separate from the WebDAV
backup status shown in settings. The immersive favorites feed stays reachable
through “沉浸播放”. Automatic follow-ups are limited to three; “加载更多”
remains available after that up to an explicitly labelled 1000-row budget.
Requests have a 15-second abort deadline; opaque cursor repeats/cycles and pages
with no new items stay retryable from the last good cursor. Deletion or confirmed
unfavorite aborts stale pages; failed favorite mutations can restore the badge
without losing the tile. A delayed home-feed retry never takes playback ownership
from this grid.

**Where the covers come from.** The Player serves `GET /api/v1/media/:id/cover`
for a media item whose committed Archive package carries a cover entry in
`manifest.json` (`media[i].cover = {path, size_bytes, mime_type, algorithm}` plus
the package-level `cover_algorithm`). The catalog records that entry in
`media_covers` (migration `0010_media_covers`) and the media DTO exposes
`cover_url` only when the row exists. URLs include an opaque `?v=` metadata
revision derived from the selected package and its committed cover declaration
(not a claimed hash of the image bytes). A changed selection gets a new URL;
a stale version returns `404` before any Archive read. The route:

- requires the Player session cookie and returns `404` when a media item has no
  cover, so a missing still is never confused with a broken one;
- reads only the declared, capped number of bytes (`MAX_COVER_BYTES`, the same
  one-megabyte budget the archive writer enforces) from the read-only WebDAV
  adapter, streams with backpressure, and fails loudly instead of sending a half
  image;
- uses its own tiny, fail-fast cover budget and **never** a playback slot
  (`/healthz` `stream_capacity.active_cover`); closes the upstream immediately
  after the byte cap, on disconnect, and even when response preparation fails;
- caches only successful, version-matched images (`private, max-age=3600`,
  `Vary: Cookie`). Errors, unversioned cover requests, and every other API
  response stay `no-store`.

An unusable cover entry (unknown algorithm, traversal path, oversized still,
non-image type) is dropped by the catalog instead of rejecting the package: a
playable video is never hidden because its thumbnail is odd. Packages archived
before the cover rollout simply have no entry, so those tiles legitimately render
the explicit “暂无封面” state - that is an honest placeholder, not a finished
thumbnail.

Long lists share the 1000-row budget and three automatic follow-ups; an explicit
load/retry control remains reachable. Requests abort on close, duplicate-only
pages stop refill, and returning restores list scroll and launched-cover focus.

For browser acceptance with generated H.264 test videos and decoded static PNG
frames, existing FFmpeg and Chrome are required:

```sh
npm run test:browser
# Optional persistent evidence directory (screenshots + report.json):
TGVIO_SCREENSHOTS=/tmp/tgvio-preview npm run test:browser

node tests/ui-acceptance.server.mjs
# Loopback only: http://127.0.0.1:5179/
# Login layout fixture (does not submit credentials):
# http://127.0.0.1:5179/tests/fixtures/sky-login.html
```

The automated runner selects an ephemeral loopback port and a temporary Chrome
profile. It checks 360/390/430/768/1440 and 844x390, state fallbacks, 150% text,
keyboard, pointer hit-testing, privacy, previews, sound confirmation and login
resize. Screenshots are synthetic test-video frames, not production catalog
content. The fixture includes deliberately missing/broken covers.

This server uses mock API/storage and real local HTTP Range media, never live
WebDAV, production login or Bot data. It removes generated files on normal exit.
A Windows CJK font may be served locally for screenshot readability, but is not
copied or included in the application. Chrome CDP touch emulation is not an
Android/iOS hardware acceptance test.

### Cache size readout

“显示缓存进度” retains the existing netSpeed preference key. Short and long
players show estimated browser-buffered size / the current rendition file size,
instead of buffered seconds or download speed. TimeRanges are merged and summed;
seek gaps are excluded. Missing size/duration is explicitly unknown, and the
estimate is not a byte-exact transfer counter or persistent offline cache.
Internal rate samples continue to drive the existing bounded preload strategy.
Quality menus list only declared 480p/720p renditions plus original; a missing
saved rendition falls back to original and is labelled accurately.

### Presentation ownership

The shell facade composes access-view, confirmations, sheet, navigation,
player-panel and timeline. LargePlayer keeps media state/events while
large-player-view builds its single stable video and grouped controls.
Library, favorites and long lists share browse-frame and styles/browse.css;
cover-image owns bounded image loading separately from tile interactions.
action-menu uses native disclosures with Escape/focus restoration.
Cache size belongs to the persistent control panel; it does not crowd the header.
The debt budget for large.ts is reduced to 682 lines after DOM extraction.

Static stills still require a real authenticated API cover_url. Better card
layout cannot create a missing archive frame, and rendition backfill does not
implicitly generate covers. Production supply evidence belongs to dated
handoffs; do not put coverage percentages or deployed versions in this guide.
