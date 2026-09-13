# R2-07A2 Archive recovery/status candidate

> Date: 2026-09-13
> Status: CANDIDATE — local implementation and fresh-image foundation passed; production cutover pending.
> Base production schema: v6 / `f4ac2877aa0379c249d703a908e4697ba4495ac183aa2450f7ffaa7897e0a244`

## Scope

R2-07A2 strengthens the existing Archive retry path without creating a second retry engine or changing the SQLite schema.

Delivered candidate behavior:

- WebDAV capability probe failure is checkpointed as `ArchivePackageState.FAILED` with normalized `archive_probe_failed`, so the periodic archive worker does not immediately spin the same runnable package forever.
- Successful capability probes are persisted in the existing `runtime_health` store under a non-secret single-profile snapshot with explicit one-hour freshness semantics.
- A failed probe records only the latest probe state and does not overwrite the last confirmed capability snapshot.
- Manual `/archive probe` uses the same durable capability/probe status path.
- Existing bounded automatic Archive recovery remains authoritative; no second retry scheduler/state machine was added.
- Existing durable object receipts remain authoritative. Stored objects are reused and are not PUT again when remote facts still confirm them.
- Manual Archive retry operation-token payload now binds the package profile/policy, event revision, every object state/retry count, and the exact failed-object index set. State changes after confirmation preparation fail closed.
- Archive retry events/logs expose only bounded non-secret counts and profile/policy identity.
- Archive mobile/status views now show profile, required/best-effort policy, confirmed/stale capability state, last probe result, stored/pending/failed object counts, and scheduled automatic retry time.

## Safety properties

- No WebDAV credential, Authorization value, endpoint URL, local path, Telegram peer/user/message identifier, caption, or source URL is added to the durable capability snapshot or user-facing status.
- Opening the Archive page only reads local SQLite/runtime-health state; it does not probe WebDAV.
- Probe failure never fabricates a successful capability state and never deletes the prior confirmed snapshot.
- Telegram publish facts/effects remain orthogonal to Archive retry and probe failures.
- This candidate is migration-free; `0001` through `0006` are unchanged and production remains schema v6 after release.

## Verification

Targeted Archive/UI suite:

- 57 tests passed.
- Includes capability freshness/profile mismatch, success-then-failure preservation, probe-failure durable checkpoint, object receipt reuse, bounded automatic recovery, Archive page rendering, owner-scoped callback behavior, and stale exact-object-set token rejection.

Fresh-image foundation:

- Docker test target rebuilt from the current source.
- `318` tests passed in `35.299s` with `--network none`.
- `foundation_gates=passed`.
- source/secret/path guard: `172` files passed.
- architecture gate: `68` Python files passed.
- `git diff --check` passed.
- schema migration diff: none.

## Production gate

Before release, production must still pass the normal read-only HostDZire preflight: one healthy instance, zero business blockers, exact v6 schema hash, clean/pushed Git identity, and safe-to-deploy=true. Release must use `migration=none`, followed by independent postflight and rollback asset verification.
