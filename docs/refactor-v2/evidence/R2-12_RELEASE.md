# R2-12 Archive Transfer Tolerance Release

> Date: 2026-09-13
> Git runtime commit: `a53b446ec405db0e2bc99817beb546511a1d85a2`
> Release: `r2-12-a53b446-20260913T125428Z`
> Migration: `none`
> Schema: v8 / `5b80e9d9aa0abe258883e5eb8b26d54ce2481b4e03daa3ee45f75e477423f76a`

## Root cause (diagnosed from openlist side)

A 2.5 GB Archive package failed with `archive_object_transfer_failed`. openlist
log evidence showed the failing object's PUT took **20m40s and only returned 201
at 19:44:09 CST**, while the app's post-PUT size verification gave up at
**19:39:18 CST** (404/not-ready). openlist → 115 upload throughput is roughly
0.1–0.3 MB/s, so the old 600s response timeout + ~6 min verification budget
aborted before the backend finished transferring. The file was not lost.

## Fix

- `TGVIO_ARCHIVE_RESPONSE_TIMEOUT_SECONDS` (default 3600) replaces the hardcoded
  600s PUT response wait.
- `TGVIO_ARCHIVE_VERIFY_ATTEMPTS` (default 120) and
  `TGVIO_ARCHIVE_VERIFY_INTERVAL_SECONDS` (default 20) give a ~40 min
  remote-size verification window; absent/404/423 are treated as "still
  transferring", so a later retry reuses a completed object
  (`archive.object.reused`) instead of failing permanently.
- `archive.object.failed` now logs `http_status`, `bytes_total` and
  `duration_ms` (redacted) for future diagnosis.
- Also fixed a real scheduler race exposed by the VPS test run: the ordered
  publish dispatcher re-reads the Job **under its phase claim** and skips if it
  is no longer `planned/publishing`, preventing a stale gate from double-running
  a non-idempotent runner.

## Verification

- local + Docker `--network none`: **382 tests**; architecture 100 files with the
  1000-line budget; secret scan and `git diff --check` clean.
- manifest `7a451a82e727198a5cb6d5e5e155add90af7dd5ff59eda25d551677819c98656`.
- The scheduler race test passes 30/30 locally after the fix.
- The first R2-12 attempt correctly fail-closed at the VPS offline test stage
  (no production mutation); the fixed commit was rebuilt and deployed.

## Production cutover

`python3 scripts/deploy_hostdzire.py --phase R2-12 --migration none`

- release `r2-12-a53b446-20260913T125428Z`, image
  `sha256:ffb787853ef4767462d30bdf6ac71c5d8321a54604b59278ec46a7a32f234404`;
- postflight: single healthy instance, restart 0, `blockers=[]`, schema v8
  unchanged, rollback-check passed.

### Rollback assets

- DB `/root/TGVIO-releases/r2-12-a53b446-20260913T125428Z/rollback/state-pre.sqlite3`
  (sha256 `5bae999d588ece25d3ef501500467d47ca9f20999c1001954d24dd206dd777bc`, v8);
- source `.../rollback/source-pre.tar.gz`
  (sha256 `d263c62acf7cea3bb1217e0fd56c634cf5ec88acad55ca8185fb8010d1d2b311`);
- `.env` backup, image tag `tgvio-rollback-pre:r2-12-a53b446-20260913T125428Z`;
  previous release `r2-11-7101d2a-20260913T122158Z`.

## Effect

The three previously failed Archive packages were retried after this fix; the
previously failing object was `reused` and the large 2.5 GB package completed.
