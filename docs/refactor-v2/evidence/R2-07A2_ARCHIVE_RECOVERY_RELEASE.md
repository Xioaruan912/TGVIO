# R2-07A2 Archive Recovery / Status Release

> Date: 2026-09-13
> Git commit: `2a074702a668b02f042406bd6fa586f9c39b98f7`
> Release: `r2-07-2a07470-20260913T025845Z`
> Schema: unchanged v6

## Delivered behavior

R2-07A2 strengthened the existing Archive retry path without introducing a second retry scheduler and without changing the database schema.

- WebDAV capability probe failure is durably checkpointed as package `FAILED` with normalized `archive_probe_failed`, preventing the archive worker from immediately spinning the same runnable package forever.
- The last successful non-secret capability snapshot is stored in existing `runtime_health` state with explicit freshness/expiry semantics.
- A later failed probe records only the latest probe result and preserves the last confirmed capability snapshot.
- Manual `/archive probe` writes the same durable probe/capability state.
- Existing bounded Archive automatic recovery remains authoritative.
- Durable object receipts remain authoritative; remotely confirmed stored objects are reused rather than blindly re-uploaded.
- Archive retry operation tokens now bind profile/policy, package/event revision, every object state/retry count, and the exact failed-object set. State changes after preparation fail closed.
- Archive pages and Job diagnostics now expose profile, policy, capability freshness, latest probe status, stored/pending/failed counts, and scheduled retry time using only local durable state.

## Verification

Candidate targeted suite:

- 57 Archive/runtime/auto-recovery/UI tests passed.
- Covered probe failure checkpointing, confirmed-capability preservation, stale capability semantics, object receipt reuse, automatic recovery, exact retry token invalidation, and Archive page rendering.

Fresh-image candidate foundation:

- `318 tests / 35.299s / OK`
- `foundation_gates=passed`
- source/secret/path guard: 172 files
- architecture gate: 68 Python files

Formal release gate:

- `318 tests / 31.409s / OK`
- `foundation_gates=passed`

## Production cutover

Preflight before cutover:

- production release: `r2-07-eadd4c9-20260913T023949Z`
- `PRAGMA user_version=6`
- schema hash: `f4ac2877aa0379c249d703a908e4697ba4495ac183aa2450f7ffaa7897e0a244`
- 24 Jobs
- all business blockers: 0
- runtime lease active: 1
- container healthy, restart count 0, error markers 0
- `safe_to_deploy=true`

Formal deployment used `migration=none`; no migration file or schema version changed.

Release identity:

- release: `r2-07-2a07470-20260913T025845Z`
- commit: `2a074702a668b02f042406bd6fa586f9c39b98f7`
- runtime image: `sha256:0012ae4f0df48c91491f31b6e4d883be39e081ba23ec3209965933c88c62634f`
- source manifest: `7790da5b41e6f3fa9e88398b839e9a8f65fcd9abea692608f4651287b89ecd4f`

Independent postflight:

- running app commit and release commit exactly match `2a074702a668b02f042406bd6fa586f9c39b98f7`
- source manifest exactly matches the release manifest
- one healthy container, restart count 0
- bootstrap marker 1, Telegram-ready marker 1, error marker 0
- 24 Jobs
- all business blockers 0
- runtime lease active 1
- `quick_check=ok`
- `PRAGMA user_version=6`
- schema hash unchanged: `f4ac2877aa0379c249d703a908e4697ba4495ac183aa2450f7ffaa7897e0a244`
- `safe_to_deploy=true`

Rollback asset validation:

`rollback_hostdzire.sh --check r2-07-2a07470-20260913T025845Z` passed.

## Remaining R2-07 scope

R2-07 remains `IN PROGRESS`. A3 exact remote Archive delete and D Diagnostic Snapshot remain. A3 production validation must not delete existing user archives; fault-matrix validation should use fake WebDAV and durable local fixtures.
