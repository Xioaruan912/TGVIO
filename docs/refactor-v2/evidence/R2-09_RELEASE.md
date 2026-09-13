# R2-09 Read-Only Operations Surface Release

> Date: 2026-09-13
> Git runtime commit: `5ff2a61e6ca51a8faaf6960d78ea34a6dbeb45af`
> Feature commit: `408e08adf34534325339067b089b8af508316eb9`
> Release-tooling closure commit: `5ff2a61e6ca51a8faaf6960d78ea34a6dbeb45af`
> Release: `r2-09-5ff2a61-20260913T092930Z`
> Feature/migration release: `r2-09-408e08a-20260913T092641Z`
> Schema: v8 / `5b80e9d9aa0abe258883e5eb8b26d54ce2481b4e03daa3ee45f75e477423f76a`

## Delivered behavior

R2-09 restores the last `REQUIRED` contract item (WB-01): a private, read-only
operations surface with authenticated metrics and a durable, redacted
notification outbox. It is decoupled from the Telegram Bot UI and never starts a
second Telegram session.

- `DashboardService` assembles JSON-safe DTOs from repository queries and the
  existing `DiagnosticSnapshotService`. It performs no external I/O and excludes
  owner/peer identifiers, captions, URLs, local paths, and credentials.
- A loopback-only stdlib `asyncio` HTTP server serves an anonymous HTML shell and
  Bearer-protected `/api/v1/overview`, `/jobs`, `/routing`, `/storage`, `/health`
  plus Prometheus `/metrics`. Only `GET`/`HEAD` are accepted; query tokens,
  request bodies, and oversized headers are rejected; every response carries
  strict security headers.
- `MetricsService` emits low-cardinality metrics; job/user/URL/error text never
  become labels.
- `notification_outbox` (migration `0008_notification_outbox`) idempotently
  mirrors terminal Job/Archive events by dedupe key and dispatches them over
  HTTPS with an HMAC-SHA256 signature, claim lease, bounded exponential retry,
  dead-lettering, and restart-safe claim recovery. Payloads use a fixed
  allowlist.
- `TGVIO_DASHBOARD_ENABLED` and `TGVIO_WEBHOOK_ENABLED` are both disabled by
  default; the dashboard host must be loopback and its token must be at least 32
  characters; webhook URLs must be credential-free HTTPS with a token.

## Changes

- New `domain/notifications.py`, `application/notifications.py`,
  `application/dashboard.py`, `application/metrics.py`,
  `infrastructure/sqlite_notifications.py`, and `adapters/web/dashboard.py`.
- `infrastructure/migrations/0008_notification_outbox.sql` adds the outbox table.
- `config.py`, `main.py`, `ports.py`, `sqlite.py`, `sqlite_observability.py`,
  `.env.example`, and `docs/ARCHITECTURE.md` are extended accordingly.
- Release tooling: `scripts/rehearse_migration.py` and
  `scripts/remote_preflight.py` now recognize the v8 schema hash.

## Migration

The release declared `0008_notification_outbox`. A production-copy rehearsal
applied `[8]` from v7 with business counts unchanged (24 Job, 44 PublishStep, 21
ArchivePackage, 187 ArchiveObject, 24 progress) and a repeat no-op. Production is
now `PRAGMA user_version=8`, ledger `1..8`, normalized schema SQL SHA-256
`5b80e9d9aa0abe258883e5eb8b26d54ce2481b4e03daa3ee45f75e477423f76a`, and the new
`notification_outbox` table is present and empty.

## Verification before cutover

- close-out gates: forbidden-path/credential scan 196 files passed; architecture
  gate 79 Python files passed; `git diff --check` and `compileall` passed.
- local clean diagnostic Docker build (`--network none`): **367 tests**; source
  manifest `1d97e920a4f14d7320778dc15d75066e482e6823ac5edc92575c0f11bbdeb2e3`.
- formal HostDZire test target under `--network none`: **367 tests**.

## Production cutover and tooling fix

The first formal invocation (`--phase R2-09 --migration 0008_notification_outbox`)
built and applied the migration correctly and recreated the single `tgvio`
service. Its final postflight, however, reported the false blocker
`database-schema` because `scripts/remote_preflight.py` carried a hardcoded
known-schema-hash map that stopped at v7. Production was healthy on v8 with all
business blockers 0; no data loss occurred.

The map was extended with the v8 hash in commit
`5ff2a61e6ca51a8faaf6960d78ea34a6dbeb45af`, pushed, and a migration-free closure
release `r2-09-5ff2a61-20260913T092930Z` was run through the same fail-closed
entry point. No manual deployment bypass, `git reset`, or running-volume
overwrite was used.

Release identity:

- runtime image: `sha256:8836cb221f446a7373087cc4cca2fdb22e777ca33d89b4b909cafd78335b740c`;
- source manifest: `1d97e920a4f14d7320778dc15d75066e482e6823ac5edc92575c0f11bbdeb2e3`;
- release test count: 367.

## Rollback assets

Closure release `r2-09-5ff2a61-20260913T092930Z` (v8 already applied):

- database: `/root/TGVIO-releases/r2-09-5ff2a61-20260913T092930Z/rollback/state-pre.sqlite3`
  (SHA-256 `fa92585e6451c1b435537347a5ac65b325f9994c3528eb8bcb141aa46b6a81fe`, v8);
- source: `/root/TGVIO-releases/r2-09-5ff2a61-20260913T092930Z/rollback/source-pre.tar.gz`
  (SHA-256 `59fc134179a51d09455f2600ca117a2256feb789104b2f1af1d79cba0e2b6092`);
- environment: `.../rollback/env-pre.bak` (mode 600);
- image tag: `tgvio-rollback-pre:r2-09-5ff2a61-20260913T092930Z`.

Schema rollback point (v7, before `0008`):

- `/root/TGVIO-releases/r2-09-408e08a-20260913T092641Z/rollback/state-pre.sqlite3`
  (v7, schema hash `9cf2d4008d4fb888f5affdffea1ccd413b51968d14d234e07d0769cfe60c6e90`),
  with image `sha256:b87fbb760d145ed6748ea61939c0d1747eddd47f2c5567a47fb18d3c237493bb`
  and previous release `r2-07d-10b6dd5-20260913T091106Z`.

## Independent postflight

`scripts/vps_check.sh` after the closure release confirmed:

- `APP_COMMIT` and release commit exactly `5ff2a61e6ca51a8faaf6960d78ea34a6dbeb45af`;
- source manifest exactly `1d97e920a4f14d7320778dc15d75066e482e6823ac5edc92575c0f11bbdeb2e3`;
- runtime image exactly `sha256:8836cb221f446a7373087cc4cca2fdb22e777ca33d89b4b909cafd78335b740c`;
- one running instance, Docker health `healthy`, restart count 0;
- bootstrap marker 1, Telegram-ready marker 1, error marker 0;
- 24 Jobs, all business blockers 0, one active runtime lease;
- `quick_check=ok`, `PRAGMA user_version=8`, schema hash unchanged;
- `blockers=[]` and `safe_to_deploy=true`.

Additional read-only checks confirmed:

- migration ledger exactly `[1, 2, 3, 4, 5, 6, 7, 8]`;
- `notification_outbox` exists with 0 rows;
- `TGVIO_DASHBOARD_ENABLED`/`TGVIO_WEBHOOK_ENABLED` are unset in the container
  and no `8787` listener is open.

Rollback asset validation passed:

`bash scripts/rollback_hostdzire.sh --check r2-09-5ff2a61-20260913T092930Z`

## Safety conclusion

R2-09 is production-delivered and WB-01 is satisfied. The read-only dashboard,
authenticated low-cardinality metrics, and the durable redacted notification
outbox are available under explicit, disabled-by-default switches. The
operations surface performs no Telegram/WebDAV side effects and never runs a
second Bot.
