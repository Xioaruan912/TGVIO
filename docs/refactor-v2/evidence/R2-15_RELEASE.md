# R2-15 Safe History Maintenance and Per-Day Display Numbers Release

> Date: 2026-09-14
> Git runtime commit: `75e6259e9212e46e002e0ce2d3b5abb22f606d8f`
> Release: `r2-15b-75e6259-20260914T012823Z` (after `r2-15a-46ae55f-20260914T011949Z`)
> Migrations: `0010_safe_history_maintenance`, `0011_display_identity`
> Schema: v11 / `344e92de1828579e46afe9df40c00ccc0eab10028718ab2d70b8aa7bb896ed6d`

## Delivered behavior (R2-15A)

- Removed the destructive `purge_terminal_history()` and the unfiltered
  `list_job_display_messages()`; terminal Job history, publish/Archive
  receipts, intake keys and audit events are never physically deleted by
  maintenance.
- `0010` adds `job_visibility`, `maintenance_runs` and `maintenance_targets`.
- `HistoryMaintenanceService`/`HistoryMaintenanceRuntime` replace the in-memory
  daily job: one durable logical run per Beijing business day (06:00 CST
  cutoff) with lease/generation takeover, frozen candidate targets, per-target
  checkpointing, bounded status-card deletion (missing = success), safe
  cache cleanup for hidden Jobs only, and expired/consumed token plus settled
  outbox recycling.
- Eligibility is fail-closed: active claim, unsettled publish step, pending
  revocation, `publish_partial`/`publish_uncertain`, required Archive not
  committed, or pending Archive deletion all keep the Job visible.
- `/jobs` gains `today` / `pending` / `history` filters; hidden Jobs leave the
  default and failure lists while remaining queryable through history.

## Delivered behavior (R2-15B)

- `0011` adds `job_display_identity(job_id, business_day, display_no)` with a
  deterministic backfill ordered by `created_at, accepted_order, rowid`.
- `display_no` is allocated inside the Job-create transaction; the permanent
  `accepted_order` stays monotonic and drives FIFO scheduling unchanged.
- `#N` resolves only within the current Beijing business day; old global
  numbers no longer silently resolve. Legacy rows without an identity fall back
  to their FIFO order for display.
- `get_stats_snapshot()` now aggregates `today_*` by business day instead of
  UTC calendar date.

## Verification

- local + Docker `--network none`: **403 tests** (R2-15A) then **405 tests**
  (R2-15B); architecture 106 Python files with the 1000-line budget; secret
  scan and `git diff --check` clean.
- R2-15A manifest `92252bb939dc85dc36f6296c833987fb8e1cc0a7d9bbfe4c1a71dda13a57d6a1`,
  image `sha256:9abbe69c684ede83a8856a88143a39c7df0f38c4a880b828cdb57fd65d16b80b`.
- R2-15B manifest `e22878365bbd0ed6f6c627afefbae1cef5abfd31d255b3a992ab868fba207d78`,
  image `sha256:d4e5ed97353a4dd14c7131ea5b2613783f2f22c246d99a245a97644c5e82b020`.
- postflight after each release: single healthy instance, restart 0,
  `blockers=[]`, `quick_check=ok`, `user_version=11`, ledger `1..11`,
  `job_visibility`/`maintenance_runs`/`maintenance_targets`/`job_display_identity`
  present, `jobs=0`; rollback-check passed for both releases.

## Rollback assets

- R2-15B: DB `.../r2-15b-75e6259-20260914T012823Z/rollback/state-pre.sqlite3`
  (sha256 `2248287b6c8ef8c547372e92c2904ece64e7bea510bab1c8875c6b6910f15677`);
  source `.../rollback/source-pre.tar.gz`
  (sha256 `54907873776f61ca4e13a826d4deda98af5c183f9377eea0a6bdd95cba2fe37a`);
  `.env` backup; image tag
  `tgvio-rollback-pre:r2-15b-75e6259-20260914T012823Z` (points at the R2-15A
  image `9abbe69c684e`); previous release `r2-15a-46ae55f-20260914T011949Z`.
- R2-15A: DB
  `.../r2-15a-46ae55f-20260914T011949Z/rollback/state-pre.sqlite3`
  (sha256 `2fdf2c7031c5b3a64db5f033a7e3fd536286baff9fa2d73beb25d588d57a29bf`);
  source `.../rollback/source-pre.tar.gz`
  (sha256 `63db602a5f485048de5758cf24eee91ba1836d040bf5b9bd609a7aa3c761f7ee`);
  image tag `tgvio-rollback-pre:r2-15a-46ae55f-20260914T011949Z` (points at the
  R2-14 image `bd0a53459e76`); previous release
  `r2-14-816409b-20260914T002637Z`.

## Note

Schema rollback requires stopping the container and restoring the matching
pre-migration DB copy; the new tables are additive and are ignored by older code
only after that restore. R2-16 (collection editing) and R2-17 (duplicate
review) remain unimplemented.
