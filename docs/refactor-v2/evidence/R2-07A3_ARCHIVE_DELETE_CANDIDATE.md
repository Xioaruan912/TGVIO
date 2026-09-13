# R2-07A3 exact remote Archive delete candidate

> Date: 2026-09-13
> Status: CANDIDATE — implementation and offline gates passed; production cutover pending.
> Baseline commit: `8db64df66b600bb06a26a2a89f60d794f4c19f0e`
> Base production schema: v6 / `f4ac2877aa0379c249d703a908e4697ba4495ac183aa2450f7ffaa7897e0a244`
> Candidate schema: v7 / `9cf2d4008d4fb888f5affdffea1ccd413b51968d14d234e07d0769cfe60c6e90`

## Scope

R2-07A3 adds exact remote Archive deletion as a separate durable destructive workflow. It does not reuse Telegram undo semantics and does not introduce recursive or prefix deletion.

Candidate behavior:

- Only a terminal job owned by the requesting user with a committed Archive package can prepare deletion.
- Preparation freezes an immutable exact target set and target-set hash, then issues an owner-scoped, revision-bound, expiring single-use operation token.
- First Telegram button press only prepares the operation and renders a second confirmation page; no remote DELETE is sent before confirmation.
- Confirmation consumes the operation once and obtains an `archive_delete` phase claim before remote work.
- Delete order is fixed as commit marker first, then recorded media objects in `object_index` order, then `manifest.json` last.
- Every target is an exact recorded file path with expected size plus available ETag/SHA-256 evidence. There is no directory, parent-root, prefix, glob, or recursive delete path.
- Each target attempt is durably checkpointed. A deleted target is monotonic and cannot be downgraded or executed again.
- Partial failure remains resumable; a later operation freezes and executes only remaining targets. The Telegram UI exposes “continue cleanup” while work remains and hides the delete action after durable completion.
- Archive package/object business rows are retained for audit; deletion lifecycle is recorded in dedicated durable tables.

## WebDAV fail-closed contract

Exact delete performs a fresh stat before DELETE and rejects collection, size, ETag, or content mismatches. When an expected SHA-256 is present, inability to read the remote content is a safety failure: no DELETE request is sent. A lost DELETE response is accepted only if a follow-up read proves the exact target is absent.

## SQLite migration

`0007_archive_exact_delete.sql` adds:

- `archive_deletions`
- `archive_deletion_targets`
- `archive_deletion_events`

Migration file SHA-256:

- `2b2dc497dc3dbb8d87eb798516e8e79f03d5b2c5ac7b62f520163ed14f9fe339`

The known v7 schema identity is registered in both migration rehearsal and remote preflight tooling. The dedicated v6→v7 rehearsal verifies:

- `from_version=6`, `to_version=7`, applied migration `[7]`;
- pre/post `PRAGMA quick_check=ok`;
- migration ledger preserved and extended through version 7;
- existing job, Archive package, and Archive object counts/states are unchanged;
- the three deletion tables are created empty;
- the pre-migration backup preserves the exact v6 schema and business counts;
- a repeated migration run is a no-op and creates no second backup.

## Test coverage added/strengthened

Telegram UI tests directly lock:

- committed terminal Archive exposes the destructive delete button;
- first press prepares only, second confirmation executes;
- cross-owner requests are rejected before preparation;
- stale and replayed confirmation tokens fail closed;
- partial deletion exposes only a continue-cleanup path and completion removes the delete action.

WebDAV tests directly lock:

- unreadable `expected_sha256` content sends zero DELETE requests;
- exact receipt verification, `If-Match`, idempotent missing target, mismatch rejection, and lost-response absence confirmation.

Repository tests directly lock:

- marker-first ordering;
- manifest-last ordering;
- deleted target monotonicity and no re-execution/downgrade.

## Verification completed before commit

- Targeted Archive/WebDAV/Telegram UI: `59` tests passed.
- Migration/release-tooling + targeted integration set: `108` tests passed.
- `scripts/check_foundation.sh`: `338` tests passed in `25.826s`; `foundation_gates=passed`.
- Independent complete offline suite: `338` tests passed in `25.554s`.
- Fresh Docker test target with `--network none`: `338` tests passed in `36.537s`; `foundation_gates=passed`.
- Dedicated v6→v7 rehearsal/no-op/v7-ledger checks: `3` tests passed.
- `git diff --check` passed.
- Source, secret/path, compile, main-entry, and architecture guards passed as part of foundation.

No test used the production Telegram session, started a second production Bot, or executed a real WebDAV DELETE.

## Production boundary

This file is candidate evidence only. R2-07A3 and AR-07 must remain incomplete until all of the following occur from a clean, pushed `origin/main` commit:

1. formal clean/pushed build gate with Docker tests under `--network none`;
2. read-only HostDZire preflight remains safe with one healthy instance and zero blockers;
3. `python3 scripts/deploy_hostdzire.py --phase R2-07A3 --migration 0007_archive_exact_delete` performs the production v6-copy rehearsal and controlled single-instance cutover;
4. independent postflight proves the exact commit/source manifest/image, SQLite v7, ledger `1..7`, v7 schema hash, healthy/restart=0, single instance, zero blockers, and all three deletion tables initially empty;
5. rollback asset check passes.

Production validation must not delete or otherwise use an existing user Archive as a destructive smoke test.
