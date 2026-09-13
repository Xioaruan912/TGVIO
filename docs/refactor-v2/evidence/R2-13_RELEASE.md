# R2-13 Archive Layout, Daily Cleanup and Runtime Toggles Release

> Date: 2026-09-13
> Git runtime commit: `9f0eb946c2aa036cc203816ea5484e0bc1a884a3`
> Release: `r2-13-9f0eb94-20260913T135837Z`
> Migration: `0009_archive_layout_flags`
> Schema: v9 / `bb9c405364ac74a758167d5b324149c0543be6812cf0e7fac3c190e52fe17a0e`

## Delivered behavior

- **Short Archive layout (`v2`)** by default: `<remote_root>/<YYYY-MM-DD>/<N>/`
  with flat `<sha256[:12]>.<ext>` files plus `manifest.json` and
  `_COMPLETE.json` (Beijing date, durable per-day sequence via
  `archive_day_counters`). Verified v1 packages are untouched.
- **Daily cleanup at 06:00 Asia/Shanghai**: clears terminal Job history
  (cascades items/events/plans/steps/effects/archive packages/display messages),
  the bot's tracked status messages, the download cache, and settled outbox
  rows; **keeps `daily_stats` and rotates JSONL logs with a 3-day retention**.
  Numbering resets to `任务 #1` when no Jobs remain. Runs once per day and also
  catches up on startup if the day's 06:00 already passed.
- **`/settings` runtime toggles** (durable `runtime_flags`): owner alerts,
  collection preview, and daily cleanup on/off; archive layout shown
  (restart-required). Defaults stay on.

## Migration

`0009_archive_layout_flags` adds `archive_day_counters` and `runtime_flags`.
A production-copy rehearsal applied `[9]` from v8 with business counts
unchanged; production is now `user_version=9`, ledger `1..9`.

## Release flow (fail-closed, then tooling fix)

The first invocation `--phase R2-13 --migration 0009_archive_layout_flags`
built and applied the migration correctly and recreated the single `tgvio`
service; production was healthy on v9 with all business blockers 0. Its final
report again hit the known tooling false positive `database-schema` because
`scripts/remote_preflight.py` did not yet know the v9 hash. Commit `9f0eb94`
added it, and a migration-free closure release completed the chain.

- release `r2-13-9f0eb94-20260913T135837Z`, image
  `sha256:5442ffa8c2f6858a9a245c0e21c042e71cc88cb336d6d9bcc1408a2c76458d58`,
  manifest `daf84fc92682494fdec13243508fce614e95c37fe884b598a7ed2cec5e3b3e8d`,
  388 tests.

## Postflight

`scripts/vps_check.sh`: single healthy instance, restart 0, `blockers=[]`,
`quick_check=ok`, `user_version=9` with unchanged v9 hash. DB read-only
inspection confirmed ledger `[1..9]`, `jobs=0` (the daily cleanup ran on the
first startup past 06:00 Beijing, as designed), empty `archive_day_counters`,
empty `runtime_flags` (defaults active) and empty outbox. Rollback-check passed.

## Rollback assets

Closure release `r2-13-9f0eb94-20260913T135837Z` (v9):

- DB `.../r2-13-9f0eb94-20260913T135837Z/rollback/state-pre.sqlite3`
  (sha256 `71f2901db98844ec5c77fc9aa86d787f448596cb167b1ca9f8c2916330d3b346`, v9);
- source `.../rollback/source-pre.tar.gz`
  (sha256 `09576e1c36904f8b3ebd37449c68c7f81f8cdee785177880d2dacc99c0e8b507`);
- `.env` backup, image tag `tgvio-rollback-pre:r2-13-9f0eb94-20260913T135837Z`.

Schema rollback point (v8, before `0009`):

- `/root/TGVIO-releases/r2-13-2043706-20260913T135625Z/rollback/state-pre.sqlite3`
  (sha256 `97197d6a1d085ff49ac5a815494be7de93850d84e95a796223cf8319a1ed3ac7`, v8),
  image `sha256:e2a9e0444602c42a528e9e4c92d28c4444dc8c5dfcf1f5ae8b030c73e0fa417c`,
  previous release `r2-12-a53b446-20260913T125428Z`.

`bash scripts/rollback_hostdzire.sh --check r2-13-9f0eb94-20260913T135837Z`
passed.
