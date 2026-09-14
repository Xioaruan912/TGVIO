# R2-14 Video Metadata, Thumbnail and Alert Correctness Release

> Date: 2026-09-14
> Git runtime commit: `816409b2ce4225ec59f0fe81eb96b915ec4a01cf`
> Release: `r2-14-816409b-20260914T002637Z`
> Migration: `none`
> Schema: v9 / `bb9c405364ac74a758167d5b324149c0543be6812cf0e7fac3c190e52fe17a0e`

## Root cause of white Telegram thumbnails

The runtime image does not install `hachoir`, which Telethon uses to parse video
metadata. As a result `telethon.utils.get_attributes(video)` collapsed to
`DocumentAttributeVideo(duration=0, w=1, h=1)`, so Telegram rendered a broken
(white) preview. The publisher also ignored the real ffprobe facts already
recorded on the `MediaItem`.

## Delivered behavior

- **Real video attributes**: `_prepare_local_media` now builds
  `DocumentAttributeVideo(duration, w, h, supports_streaming)` from the probed
  `MediaItem` fields and overwrites Telethon's hachoir-dependent guess. Verified
  against a real cached video (`720x1280`, `116.7s`).
- **Robust thumbnails**: candidate frames now sample 5%–80% plus fallbacks;
  near-white/blank frames are rejected in addition to black frames; the JPEG
  budget was raised to **1 MB**; a missing thumbnail is logged
  (`publish.thumbnail.missing`). Correct video attributes are always sent even
  when no thumbnail is available.
- **Alerts only on final failure**: notification candidates now carry the durable
  recovery status; `job.failed`/`archive.failed` are enqueued only when the
  failure is final (`exhausted/abandoned/quarantined/manual_review`). Transient
  failures (`scheduled`/`retrying`) no longer page the owner, so a self-healing
  Archive retry stays silent.
- **Environment self-check**: a new `probe_environment_capabilities()` runs at
  startup, is logged as `runtime.selfcheck`, stored as the `capabilities`
  runtime-health row, and rendered in `/diag`
  (`ffmpeg/ffprobe/yt-dlp/cryptg/hachoir`).
- **Metrics fix**: `tgvio_process_resident_memory_bytes` now converts Linux
  `ru_maxrss` (KiB) to bytes.

## Diagnosis of the 2026-09-13 Archive failures

Logs (retained; only DB records were cleared at 06:00) show three packages
failed transiently and then **all completed**:
`arc_4334612…` (2.5 GB, PUT `TimeoutError`, fixed in R2-12), `arc_2ba5fd…`
(object 7 HTTP 405 then PUT 201 + MOVE 201), `arc_11a434…` (object 2 HTTP 405,
auto-recovery retry reused stored objects and completed). The 405s came from
openlist/115 (including `MKCOL` 405 on staging paths). Because these were
transient, they must not alert; R2-14 enforces that.

## Verification

- local + Docker `--network none`: **397 tests**; architecture 104 files with
  the 1000-line budget; secret scan and `git diff --check` clean.
- manifest `23a08a0093bfe06c4148f3e9080ec7c5646886290fe1d762438bd7dcf525b488`.
- postflight: single healthy instance, restart 0, `blockers=[]`, schema v9
  unchanged; `runtime_health.capabilities = {ffmpeg:true, ffprobe:true,
  yt_dlp:true, cryptg:true, hachoir:false}`; rollback-check passed.

## Rollback assets

- DB `.../r2-14-816409b-20260914T002637Z/rollback/state-pre.sqlite3`
  (sha256 `32b60fcb759465cdf9c8f8d6096de2cdd24eba748d16162e536e014fa9401330`, v9);
- source `.../rollback/source-pre.tar.gz`
  (sha256 `43a368387150fd1dfa2e2e36bd4308a02e787d689cfdefe5879103eeebc4bd3d`);
- `.env` backup, image tag `tgvio-rollback-pre:r2-14-816409b-20260914T002637Z`;
  previous release `r2-13-9f0eb94-20260913T135837Z`.

## Note

Existing messages published before this fix keep their white preview; the owner
must re-forward a video for the corrected attributes/thumbnail to apply (Telegram
does not recompute old messages).
