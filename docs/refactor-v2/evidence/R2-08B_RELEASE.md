# R2-08B Hotspot Split Completion Release

> Date: 2026-09-13
> Git runtime commit: `52f39cad6c33614daaa742ed2f07dea0c4a6011d`
> Release: `r2-08b-52f39ca-20260913T101837Z`
> Migration: `none`
> Schema: v8 / `5b80e9d9aa0abe258883e5eb8b26d54ce2481b4e03daa3ee45f75e477423f76a`

## Delivered behavior

R2-08B finishes the R2-08 hotspot splits and tightens the source-size budget.
It is behavior-preserving: no schema, callback, UI text, or state-machine
change; all 368 tests pass unchanged.

Splits (mixin extraction, exceptions/protocols moved into shared
`*_support.py` modules so their class identity is preserved):

- `adapters/telegram/publish_transport.py` 822 → 483 lines:
  `publish_albums.py`, `publish_references.py`, `publish_large_files.py`,
  `publish_transport_support.py`.
- `adapters/webdav_archive.py` 648 → 428 lines:
  `webdav_client.py` (HTTP/DAV mechanics + path policy) and
  `webdav_archive_support.py`.
- `application/archive_executor.py` 560 → 390 lines:
  `archive_commit.py` (manifest/marker/final verify),
  `archive_probe.py` (capability/probe checkpoint),
  `archive_executor_support.py`.
- `infrastructure/sqlite_archive.py` 958 → 499 lines:
  `sqlite_archive_deletion.py` (deletion targets/events/checkpoint); the
  repository facade `sqlite.py` now inherits both mixins.
- `adapters/telegram/intake_runtime.py` 1467 → 945 lines:
  `intake_status.py` (live status) and `intake_collection.py` (collection UI);
  `intake_runtime_support.py` carries `_PendingBatch`.
- `adapters/telegram/bot_ui_jobs.py` 1085 → 467 lines:
  `bot_ui_job_actions.py` (retry/cancel/hold/resume/undo actions).

The largest remaining source file is 945 lines. `scripts/release_guard.py`
`MAX_SOURCE_FILE_LINES` was tightened from 1600 to **1000**; the architecture
gate now reports 98 Python files and the 1000-line budget.

`application/commands` + `application/queries` were intentionally **not**
reshuffled into literal directories: TGVio already separates commands
(application services) from queries (`application/ports.py` query methods over
repository read models). This is recorded here rather than performing an
invasive, behavior-neutral directory move.

## Verification before cutover

- close-out gates: forbidden-path/credential scan 218 files passed; architecture
  gate 98 Python files passed with the 1000-line budget; `git diff --check` and
  `compileall` passed.
- local clean diagnostic Docker build (`--network none`): **368 tests**; source
  manifest `9d2b27c3b058c5ee2de70a34e4472eab3c6bdc354fd882bb2c2b3342fe394fb9`.
- formal HostDZire test target under `--network none`: **368 tests**.

## Production cutover

`python3 scripts/deploy_hostdzire.py --phase R2-08B --migration none`

- release: `r2-08b-52f39ca-20260913T101837Z`;
- commit: `52f39cad6c33614daaa742ed2f07dea0c4a6011d`;
- source manifest: `9d2b27c3b058c5ee2de70a34e4472eab3c6bdc354fd882bb2c2b3342fe394fb9`;
- runtime image: `sha256:507d38f97c3d5d3d60d2d2295ec2c5e850d053554e70cc6af73d9ea6bdf3bd0e`;
- release test count: 368.

No schema change; production remains `user_version=8`.

## Rollback assets

- database: `/root/TGVIO-releases/r2-08b-52f39ca-20260913T101837Z/rollback/state-pre.sqlite3`
  (SHA-256 `e8641b178fda9fb1c12c059af9da828492371138ad3ce565f55566e7c0d29398`, v8);
- source: `/root/TGVIO-releases/r2-08b-52f39ca-20260913T101837Z/rollback/source-pre.tar.gz`
  (SHA-256 `9d3ed8c5f63d31a2788323fcca7f6364e3d9b6d7b89369ee1604e69f6563815a`);
- environment: `.../rollback/env-pre.bak` (mode 600);
- image tag: `tgvio-rollback-pre:r2-08b-52f39ca-20260913T101837Z`
  (previous release `r2-08-c39fe9d-20260913T094000Z`, image
  `sha256:1f931e65a76d5ad3c90cc81ccdd3a07fce10034d7c6ea802b2ba0d494e44111f`).

## Independent postflight

`scripts/vps_check.sh` after cutover confirmed one healthy single instance,
restart count 0, `blockers=[]`, `safe_to_deploy=true`, `quick_check=ok`,
`user_version=8` with unchanged schema hash, and 24 Jobs with all business
blockers 0. Rollback asset validation passed:

`bash scripts/rollback_hostdzire.sh --check r2-08b-52f39ca-20260913T101837Z`
