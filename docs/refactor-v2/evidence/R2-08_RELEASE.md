# R2-08 Hotspot Split Release

> Date: 2026-09-13
> Git runtime commit: `c39fe9d579d86e73c1505741606221408e0b5fd2`
> Split commit: `1c26f733fca819164edb3321040cc19a68d9609a`
> Release: `r2-08-c39fe9d-20260913T094000Z`
> Migration: `none`
> Schema: v8 / `5b80e9d9aa0abe258883e5eb8b26d54ce2481b4e03daa3ee45f75e477423f76a`

## Delivered behavior

R2-08 begins the hotspot governance phase. It is a behavior-preserving refactor
of the largest Bot UI file and adds an automated source-size budget to the
architecture gate.

- `adapters/telegram/bot_ui.py` dropped from **3324** lines to **867** lines.
- Its members were moved verbatim into cohesive mixins:
  - `bot_ui_jobs.py` (1086 lines, 37 methods): job list/detail/failure/plan
    rendering plus retry/cancel/hold/resume/undo action callbacks.
  - `bot_ui_format.py` (768 lines, 31 methods): pure presentation helpers and
    button builders.
  - `bot_ui_archive.py` (597 lines, 11 methods): Archive text/page/command and
    delete/retry/probe callbacks.
  - `bot_ui_fixture.py` (232 lines, 4 methods): controlled fixture publish flow.
  - `bot_ui_support.py` (78 lines): shared UI constants.
- `TelethonBotUI` now inherits the mixins; no call site, callback data, string,
  or behavior changed. All 368 tests pass unchanged.
- `scripts/release_guard.py` `check_architecture` now rejects any `src/tgvio`
  Python file longer than `MAX_SOURCE_FILE_LINES = 1600`, in addition to the
  existing AST dependency-boundary rules. The gate returns
  `max_source_file_lines` in its report.

No Telegram/WebDAV/business state machine, schema, UI text, or callback contract
changed. The repository split was already delivered in R2-03B; this release
addresses the Bot UI hotspot and locks file-size budgets into CI.

## Verification before cutover

- close-out gates: forbidden-path/credential scan 202 files passed; architecture
  gate 84 Python files passed with the new 1600-line budget; `git diff --check`
  and `compileall` passed.
- local clean diagnostic Docker build (`--network none`): **368 tests**; source
  manifest `ba8ad0406e1a32d1a921dd98a0860f860731b006e5fc0f32e37dbb23e94982c0`.
- formal HostDZire test target under `--network none`: **368 tests**.

## Production cutover

Formal deployment used the approved entry point:

`python3 scripts/deploy_hostdzire.py --phase R2-08 --migration none`

Release identity:

- release: `r2-08-c39fe9d-20260913T094000Z`;
- commit: `c39fe9d579d86e73c1505741606221408e0b5fd2`;
- source manifest: `ba8ad0406e1a32d1a921dd98a0860f860731b006e5fc0f32e37dbb23e94982c0`;
- runtime image: `sha256:1f931e65a76d5ad3c90cc81ccdd3a07fce10034d7c6ea802b2ba0d494e44111f`;
- release test count: 368.

No schema change; production remains `user_version=8`.

## Rollback assets

- database: `/root/TGVIO-releases/r2-08-c39fe9d-20260913T094000Z/rollback/state-pre.sqlite3`
  (SHA-256 `064dddd8951a0ec6cce9f1f3e830b36daa3bdc6f3bfc80e3c3d403182456d7a8`, v8);
- source: `/root/TGVIO-releases/r2-08-c39fe9d-20260913T094000Z/rollback/source-pre.tar.gz`
  (SHA-256 `7902ce023fd4d10fa1b16a6c91d064725bb1f0d5fd7fc287cc23cf949c8f0179`);
- environment: `.../rollback/env-pre.bak` (mode 600);
- image tag: `tgvio-rollback-pre:r2-08-c39fe9d-20260913T094000Z`
  (previous release `r2-09-5ff2a61-20260913T092930Z`, image
  `sha256:8836cb221f446a7373087cc4cca2fdb22e777ca33d89b4b909cafd78335b740c`).

## Independent postflight

`scripts/vps_check.sh` after cutover confirmed:

- `APP_COMMIT` and release commit exactly `c39fe9d579d86e73c1505741606221408e0b5fd2`;
- source manifest exactly `ba8ad0406e1a32d1a921dd98a0860f860731b006e5fc0f32e37dbb23e94982c0`;
- runtime image exactly `sha256:1f931e65a76d5ad3c90cc81ccdd3a07fce10034d7c6ea802b2ba0d494e44111f`;
- one running instance, Docker health `healthy`, restart count 0;
- bootstrap marker 1, Telegram-ready marker 1, error marker 0;
- 24 Jobs, all business blockers 0, one active runtime lease;
- `quick_check=ok`, `PRAGMA user_version=8`, schema hash unchanged;
- `blockers=[]` and `safe_to_deploy=true`.

Rollback asset validation passed:

`bash scripts/rollback_hostdzire.sh --check r2-08-c39fe9d-20260913T094000Z`

## Residual hotspots

`adapters/telegram/intake_runtime.py` (1467 lines) and
`infrastructure/sqlite_archive.py` (958 lines) remain the largest files and are
now bounded by the 1600-line budget. Further Telegram/WebDAV/Archive executor
splitting belongs to the ongoing R2-08/R2-10 governance work.
