# R2-07A3 Exact Remote Archive Delete Release

> Date: 2026-09-13
> Git runtime commit: `98e2aaac4598315b23e8224d4e6c888db155aef0`
> A3 implementation commit: `6c3d9a93cc0b45aca13d243c247386fbbb5e93cd`
> Release-tooling compatibility commit: `98e2aaac4598315b23e8224d4e6c888db155aef0`
> Release: `r2-07a3-98e2aaa-20260913T072332Z`
> Schema: v7 / `9cf2d4008d4fb888f5affdffea1ccd413b51968d14d234e07d0769cfe60c6e90`

## Delivered behavior

R2-07A3 adds exact remote Archive deletion as a durable, fail-closed destructive workflow while keeping Telegram publication and local audit history intact.

- Only the owner of a terminal Job with a committed ArchivePackage can prepare deletion.
- Telegram requires a distinct second confirmation. The first click only prepares an owner/revision/TTL/single-use operation token; no remote DELETE occurs before confirmation.
- Preparation freezes an immutable target set. Execution is limited to the exact recorded package/object receipts and never performs directory, parent-root, prefix, glob, or recursive deletion.
- Delete order is fixed as `_COMPLETE.json` first, recorded media objects by `object_index`, and `manifest.json` last.
- The repository independently enforces marker-first and manifest-last ordering even if a caller attempts to bypass the application service.
- Every target is checkpointed. Once a target is durably `deleted`, it cannot be downgraded or executed again.
- Partial failure is resumable: later confirmation freezes and executes only remaining targets.
- WebDAV delete verifies current remote facts before the destructive request. Size, ETag, collection, and expected SHA-256 mismatches fail closed. If expected content cannot be read for SHA-256 verification, zero DELETE requests are sent.
- Lost DELETE responses are accepted only after a follow-up read proves the exact target is absent.
- Telegram UI distinguishes fresh deletion, partial cleanup, and completed deletion; completed packages no longer expose the destructive action.

## Migration

The release declared and applied `0007_archive_exact_delete`.

Migration file SHA-256:

- `2b2dc497dc3dbb8d87eb798516e8e79f03d5b2c5ac7b62f520163ed14f9fe339`

New durable tables:

- `archive_deletions`
- `archive_deletion_targets`
- `archive_deletion_events`

The production migration ledger is now exactly `1..7` and the normalized v7 schema SQL SHA-256 is `9cf2d4008d4fb888f5affdffea1ccd413b51968d14d234e07d0769cfe60c6e90`.

## Verification before cutover

A3 implementation verification before its first commit:

- targeted Archive/WebDAV/Telegram UI: 59 tests passed;
- migration/release-tooling plus targeted integration set: 108 tests passed;
- foundation: 338 tests passed in 25.826s;
- independent complete offline suite: 338 tests passed in 25.554s;
- Docker test target under `--network none`: 338 tests passed in 36.537s;
- dedicated v6→v7 rehearsal/no-op/v7-ledger checks: 3 tests passed;
- `git diff --check`, source/secret/path, compile, main-entry, and architecture gates passed.

The initial clean/pushed build gate for implementation commit `6c3d9a93cc0b45aca13d243c247386fbbb5e93cd` passed 338 tests and runtime image inspection.

The first deployment invocation then correctly failed before any production mutation because the release tooling only accepted `R2-NN` phase identifiers while the approved command required `R2-07A3`. The release-ID validators were fixed consistently across local deploy, remote release, previous-source rollback safety checks, and rollback-check. No manual deployment bypass was used.

Final release-tooling gate on runtime commit `98e2aaac4598315b23e8224d4e6c888db155aef0`:

- foundation: 339 tests passed in 26.943s;
- formal clean/pushed Docker build gate: 339 tests passed in 38.201s;
- source manifest: `37cac7ac97c6ad1bbfc2403e0f3719ecc33b0b2016de2afd51da49a3fd1421a0`;
- local runtime image inspection passed;
- architecture gate: 69 Python files passed.

All Docker test execution used `--network none`; no second production Bot or production Telegram session was used.

## Production preflight

Immediately before cutover, the existing A2 production state was independently read-only checked:

- release `r2-07-2a07470-20260913T025845Z`;
- commit `2a074702a668b02f042406bd6fa586f9c39b98f7`;
- SQLite `user_version=6`;
- v6 schema hash `f4ac2877aa0379c249d703a908e4697ba4495ac183aa2450f7ffaa7897e0a244`;
- `quick_check=ok`;
- 24 Jobs;
- all business blockers 0;
- one active runtime lease;
- one healthy container, restart count 0;
- `safe_to_deploy=true`.

The HostDZire report still showed `ntp_synchronized=no`; this remained a known non-blocking host maintenance item.

## Production cutover

Formal deployment used only the approved entry point:

`python3 scripts/deploy_hostdzire.py --phase R2-07A3 --migration 0007_archive_exact_delete`

Release identity:

- release: `r2-07a3-98e2aaa-20260913T072332Z`;
- commit: `98e2aaac4598315b23e8224d4e6c888db155aef0`;
- source manifest: `37cac7ac97c6ad1bbfc2403e0f3719ecc33b0b2016de2afd51da49a3fd1421a0`;
- runtime image: `sha256:c432d3aaf8314214a37a321c5e58a43a21e0fe728f89cb1b994d70d40aad7eee`;
- release test count: 339.

The release path performed the controlled production v6-copy migration rehearsal and the normal backup/rollback preparation before the single-instance recreate. No existing user Archive was used as a destructive smoke test and no real WebDAV DELETE was manually issued during release validation.

## Independent postflight

`scripts/vps_check.sh` after cutover confirmed:

- running `APP_COMMIT` and release commit exactly `98e2aaac4598315b23e8224d4e6c888db155aef0`;
- source manifest exactly `37cac7ac97c6ad1bbfc2403e0f3719ecc33b0b2016de2afd51da49a3fd1421a0`;
- runtime image exactly `sha256:c432d3aaf8314214a37a321c5e58a43a21e0fe728f89cb1b994d70d40aad7eee`;
- one running instance, Docker health `healthy`, restart count 0;
- bootstrap marker 1, Telegram-ready marker 1, error marker 0;
- 24 Jobs and all business blockers 0;
- one active runtime lease;
- `quick_check=ok`;
- `PRAGMA user_version=7`;
- schema hash exactly `9cf2d4008d4fb888f5affdffea1ccd413b51968d14d234e07d0769cfe60c6e90`;
- `safe_to_deploy=true`.

A separate read-only aggregate query confirmed:

- migration ledger versions exactly `[1, 2, 3, 4, 5, 6, 7]`;
- `archive_deletions=0`;
- `archive_deletion_targets=0`;
- `archive_deletion_events=0`.

This proves migration installation did not synthesize deletion work for existing user Archives.

Rollback asset validation also passed:

`bash scripts/rollback_hostdzire.sh --check r2-07a3-98e2aaa-20260913T072332Z`

## Safety conclusion

R2-07A3 is production-delivered. AR-07 exact remote deletion is now covered by owner-scoped confirmation, immutable exact target enumeration, marker-first/manifest-last repository enforcement, durable per-target audit/checkpointing, partial resume, and WebDAV fail-closed verification.

R2-07 remains `IN PROGRESS` only for the retained R2-07D Diagnostic Snapshot / static proxy diagnostic work. Dynamic Destination Profiles and dynamic Proxy Profiles remain retired.
