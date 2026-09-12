# R2-06C Job Query / Failure Center Candidate

Date: 2026-09-12
Status: LOCAL CANDIDATE — not released
Base: `b59897a50a8551d4192f19d232755993f5b24ca3`
Schema: unchanged from R2-06B v4 candidate (`user_version=4`)

## Scope

This package completes the read/query portion of the R2-06 operator UX without adding another migration or mixing in operation tokens / undo.

Delivered:

- SQL-paged `/jobs` query (`COUNT + LIMIT/OFFSET`) with owner isolation.
- Durable filters: `all`, `active`, `held`, `failed`, `completed`.
- Five rows per Telegram page by default; page requests are clamped to a valid page.
- Held jobs use `job_controls.hold_requested`; there is no fake `PAUSED` JobState.
- Telegram task list exposes filter buttons, previous/next/refresh controls and direct detail buttons.
- Dedicated failure center with SQL paging.
- Failure center hides failures that are still owned by bounded auto-recovery.
- Final/manual-review Job failures and final Archive failures are surfaced.
- `publish_partial` / `publish_uncertain` remain explicit manual-review conditions and are never presented as safely retryable.
- Archive-only failure can be surfaced even when Telegram publishing already succeeded.
- Existing task diagnostics remain the single task-detail implementation; this package does not create a second detail stack.
- Help text now explains filters and failure center.

Explicitly NOT included:

- `operation_tokens`
- undo / Telegram message deletion
- schema changes
- new external services

## Query semantics

### Job filters

- `all`: every Job owned by the Telegram user.
- `active`: non-terminal Jobs that are not held.
- `held`: Jobs with durable `job_controls.hold_requested=1`.
- `failed`: JobState `failed`, including failures still being automatically recovered (normal task history view).
- `completed`: `succeeded` or `cancelled`.

Production SQLite performs filtering before pagination. The Bot does not load the complete Job history and slice it in Python.

### Failure center

The failure center is intentionally different from the general `failed` filter. It lists only failures that need human attention:

- unmanaged ordinary Job failure;
- auto-recovery Job failure after `abandoned` / `exhausted`;
- `manual_review` / `quarantined` publish failure;
- unmanaged ArchivePackage failure;
- Archive auto-recovery after `abandoned` / `exhausted`.

A policy-managed failure with an empty/scheduled/in-progress recovery status is excluded until automatic recovery reaches a final state.

Ordering prioritizes potential visible Telegram side effects (`publish_partial` / `publish_uncertain`), then other Job failures, then Archive-only failures.

## Safety properties

- All production Job-page SQL includes `owner_id=?`.
- Failure-center SQL also includes `owner_id=?`; another user's failure cannot appear in the page.
- Callback payloads remain below Telegram's 64-byte limit.
- List/filter/failure-center callbacks are read-only.
- Action buttons continue to route through existing owner-checked Job detail handlers.
- No automatic retry rule was widened.
- No Archive retry rule was widened.
- No migration file changed in this package.

## Validation

Focused repository/UI/release tests:

- 54 tests passed.
- Owner isolation covered.
- Active/held/failed/completed filters covered.
- Page clamping covered.
- Failure center excludes auto-recovery pending Job and Archive failures.
- Failure center includes exhausted/manual-review failures.
- 1000-Job query test reads one five-row SQL page.
- Telegram filter/failure callbacks and callback-size limits covered.

Formal isolated foundation gate from a test image built from this worktree:

- `274 tests in 31.186s`
- `OK`
- `foundation_gates=passed`
- network disabled for test execution.

Architecture candidate count: 62 Python files (one new pure-domain query model).

## Release dependency

R2-06C must not be released before R2-06B (`0004_queue_controls`) lands in production because the `held` query depends on the v4 `job_controls.hold_requested` column.

R2-06C itself is migration-free. Once production is on v4 and R2-06B postflight / rollback checks are complete, this isolated candidate can be cherry-picked or rebased onto clean `main`, revalidated from the pushed commit, and released with `migration=none`.
