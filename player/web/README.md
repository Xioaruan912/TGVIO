# TGVIO Player Web

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
