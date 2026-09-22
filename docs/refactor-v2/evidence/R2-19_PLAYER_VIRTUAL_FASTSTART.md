# R2-19 Player Virtual Faststart (long clips)

Release date: 2026-09-22

## Problem

A long clip (e.g. an 18-minute video) stayed on a black loading screen, and
next-clip caching was ineffective. Root cause: every archived MP4 is a
**non-faststart** progressive file whose `moov` atom sits at the end of the
file. Verified on production: a 47-minute / 731 MB file is laid out
`ftyp → free → mdat(731 MB) → moov(last ~2.4 MB)`; an 11-second file is the same
shape. A browser must read the trailing `moov` before it can render a frame, so
it first streams `bytes=0-` from the start (the whole file) and then fetches the
tail — tens of seconds of black screen on the ~2 MB/s archive backend, and some
Chromium builds never issue the tail request at all (chromium issue 40773831).

The Bot already remuxes with `-movflags +faststart` when publishing to Telegram,
but the **archive stores the original bytes** (`ArchivePlanner._canonical_path`
uses `item.local_path`), so the Player always received non-faststart files.

The link path was ruled out: `csdn.im` resolves directly to the origin (no
Cloudflare), and host→container / public / WebDAV throughput are all ~2 MB/s.
Pointing at the host IP is not faster.

## Fix: server-side virtual faststart (no transcode, no re-download)

`src/tgvio_player/application/faststart.py`
- Parses top-level MP4 boxes with at most two range reads (a 64 KiB head window
  plus, when needed, the trailing `moov`), relocates `moov` to the front in a
  virtual layout `[prefix][moov][data]`, and rewrites only the absolute chunk
  offsets (`stco` 32-bit / `co64` 64-bit) by `+moov_len`. Media bytes are never
  copied; the virtual file is the same length as the original.
- Unsupported layouts (already faststart, fragmented `moof`, trailing boxes,
  out-of-range offsets) return "no overlay" and the original stream is used.
  Remote read failures are transient and retried, never cached as "unusable".
- `FaststartOverlay` serves the whole front (prefix + patched `moov`) from cache,
  so a request for the start of a clip never touches the remote store.
- `FaststartService` keeps a byte-bounded in-memory LRU plus a file cache
  (`<data_dir>/faststart/<media_id>.bin`), keyed by the content-addressed
  `media_id`.
- `FaststartBackfill` builds every active clip's overlay in the background with
  a bounded build budget, pausing whenever a playback stream is active.

`src/tgvio_player/infrastructure/faststart_store.py` — atomic file cache.

`src/tgvio_player/adapters/http/server.py`
- When an overlay is ready, `_stream` serves the virtual range; otherwise it
  attempts a short, **playback-prioritised** build (bounded, off the stream
  slot budget) and falls back to the original file if it is not ready.
- `POST /api/v1/media/{id}/prepare` schedules a background build.
- `/api/v1/feed` schedules builds for the first clips.

`player/web`: non-active `<video>` slots use `preload="none"`; after the active
clip plays, the client calls `prepare` for N+1..N+3; a first-frame stall guard
warns at 12s and skips at 30s instead of showing an endless black screen.

## Verification

Offline: `npm run build` passes; full repository **788 passed** (was 777);
`release_guard.py verify-tree` (317 files) and `architecture` (169 files) pass;
`git diff --check` clean.

Production (`https://csdn.im`, real API/media/WebDAV):
- 47-minute / 731 MB clip: stream response now starts `ftyp → free → moov`
  (`moov` at byte 44) instead of at the end; metadata ready and playback in
  **~2s** (single `bytes=0-` request, no tail seek). Previously tens of seconds
  or never.
- Cached overlay requests: **1-2 ms**.
- Cold (uncached) clip: first frame ~4s, then cached.
- Mobile 390x844 and 430x932: `429` across initial load / 12 slow swipes /
  25 rapid swipes = **0 / 0 / 0**; real playback observed; `video` elements <= 3;
  playing <= 1; full-height viewport; no horizontal overflow.
- Static assets immutable, `/healthz` no-store, `/` no-cache, unauthenticated
  feed `401`.

Deployment: Player-only. New image `tgvio-player:r2-19-wait6` (revision
`c4ffdd0`), healthy, `restarts=0`; Bot container `83e013cc...` unchanged,
`restarts=0`; `bot_container_unchanged=true`. Rollback image
`tgvio-player:rollback-r2-19-wait6`.

## Remaining

- Background overlay backfill for all 886 clips runs opportunistically (it
  pauses during playback), so newly seen clips may still take a cold ~4s once.
- HEVC clips (142) remain undecodable on many Android/Chromium builds; the
  overlay does not change that.
- `~2 MB/s` archive throughput is an infrastructure limit.
