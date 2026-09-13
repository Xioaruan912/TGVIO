# R2-10 Closure Report

> Date: 2026-09-13
> Runtime commit: `c39fe9d579d86e73c1505741606221408e0b5fd2`
> Production release: `r2-08-c39fe9d-20260913T094000Z`
> Schema: v8 / `5b80e9d9aa0abe258883e5eb8b26d54ce2481b4e03daa3ee45f75e477423f76a`

## Status

R2-10 is closed for everything that can be delivered and verified without the
owner's live Telegram account:

- `FEATURE_CONTRACT.md` has no `REQUIRED` items left; every remaining row is
  `VERIFIED`, `COVERED`, or an explicit `RETIRED` decision.
- The full automated matrix passes on the exact runtime commit (368 tests,
  `--network none`).
- The retired legacy runtime is not present in the startable tree and is
  preserved only by the annotated tag `legacy-telegram-video-forwarder-750b3c1`.
- Release identity, rollback assets, schema ledger, and single-instance state are
  independently verified.

The controlled production smoke (real media through the channel/discussion and
the destructive undo/Archive actions) and a destructive HostDZire rollback drill
require the owner's Telegram session, so they are listed below as explicit
user-verification steps rather than silently claimed.

## Automated acceptance matrix (on `c39fe9d`)

| Area | Evidence |
|---|---|
| Full offline suite | 368 tests, Docker `--network none`, Bot disabled |
| State machine + revision CAS | `test_repository`, `test_scheduler`, `test_job_control` |
| Migration ledger + rehearsal | `test_migrations` (v8, ledger `1..8`, rollback/no-op/tamper) |
| 100 Job FIFO + 1000 item query | `test_repository_scaling`, `test_scheduler` |
| Crash/restart recovery | `test_auto_recovery`, `test_archive_runtime`, `test_shutdown` (repository-driven) |
| Disk guard + cache cleanup | `test_cache_cleanup` |
| Network/partial publish | `test_publish_pipeline`, `test_errors` equivalents |
| Archive lost-response + exact delete | `test_webdav_archive`, `test_archive_deletion` |
| UI owner/callback + 64-byte | `test_bot_ui`, `test_dashboard` |
| Read-only ops surface | `test_dashboard`, `test_notifications`, `test_metrics` |
| Architecture/source budget | `release_guard architecture` (84 files, max 1600 lines) |

## Production state (read-only verified)

- Single `tgvio` container, `running`/`healthy`, restart count 0, one instance.
- `APP_COMMIT` = `c39fe9d579d86e73c1505741606221408e0b5fd2`; source manifest
  `ba8ad0406e1a32d1a921dd98a0860f860731b006e5fc0f32e37dbb23e94982c0`; image
  `sha256:1f931e65a76d5ad3c90cc81ccdd3a07fce10034d7c6ea802b2ba0d494e44111f`.
- SQLite `quick_check=ok`, `user_version=8`, ledger `[1..8]`, 24 Jobs, all
  business blockers 0; `notification_outbox` present and empty.
- `bash scripts/rollback_hostdzire.sh --check r2-08-c39fe9d-20260913T094000Z`
  passed.
- No second Bot/session, no test artifacts in the runtime image, no public
  listener (dashboard/webhook unset).
- Known non-blocking host item: `ntp_synchronized=no`.

## Legacy retirement

- `src/` contains only `tgvio/`; the old `src/bot.py`-style entry does not exist.
- The retired runtime is archived by tag
  `legacy-telegram-video-forwarder-750b3c1` and must not be mixed back into the
  package.
- `docs/REFACTORING.md` and `docs/R0_BASELINE.md` remain as historical records;
  `docs/refactor-v2/` is the authoritative entry.

## User verification required (owner Telegram session)

These are the only unfinished R2-10 items and cannot be performed by an agent.

1. **Controlled publishing smoke** (use throwaway media, then delete the test
   posts):
   - single video and single photo;
   - a native album;
   - an explicit `/begin` … `/end` collection with text comments;
   - cover mode: channel cover + discussion-thread video groups;
   - spoiler (`always_spoiler`) and non-spoiler;
   - a URL intake (yt-dlp) task;
   - a >2GB item (playable video segments / binary volumes + manifest).
2. **Control actions**: cancel a running job, `hold`/`resume`, retry a failed
   upload, and confirm global queue pause/resume.
3. **Destructive undo**: publish a test Job, use `↩️ 撤销发布`, confirm the
   confirmation page, and verify the channel/discussion messages are deleted and
   audited.
4. **Archive**: confirm a package reaches `committed`, then exercise the exact
   owner-scoped Archive remote delete on a throwaway package.
5. **Large-file performance** (R2-04E): observe 1–3 real large uploads for
   throughput/CPU/memory/FloodWait.
6. **Rollback drill** (separate maintenance window): perform one
   `rollback_hostdzire.sh --execute` to the previous release and then forward
   again, recording recovery time and the data boundary. Do not run this without
   an explicit window; it briefly reverts production code.

Report only non-secret outcomes (counts, states, timings). Never paste tokens,
session data, captions, or local paths.
