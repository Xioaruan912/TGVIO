# R2-07D Diagnostic Snapshot Candidate

> Status: candidate — not yet a production release
> Date: 2026-09-13
> Intended release phase: `R2-07D`
> Migration: `none`

## Scope

This candidate implements the retained R2-07D scope only: a fixed, local-only,
redacted diagnostic snapshot and a deployment-only static-proxy reachability
status. It does not add a listener, Dashboard, proxy profile, proxy pool,
runtime proxy switching, destination profile, or any Bot credential workflow.

## Diagnostic contract implemented

- The snapshot is a stable domain DTO with a fixed allowlist: release identity,
  schema/migration verification, runtime-lease summary, scheduler aggregates,
  Archive aggregates/capability freshness, non-sensitive feature flags, and
  static-proxy state/check time.
- Scheduler and Archive values use three bounded SQLite aggregate queries; the
  snapshot does not load a Job history, media records, captions, peer/user
  identifiers, or paths.
- Runtime-health details are never rendered wholesale. The proxy projection
  accepts only its enum state and a bounded epoch timestamp; bad values become
  safe unknown/unchecked values.
- Identity values from the runtime environment are regex-validated before
  rendering. Exceptions are normalized to component availability states rather
  than passing through exception text.
- Bare `/diag` and the mobile “技术诊断” button call only the snapshot service.
  They do not call Telegram, WebDAV, cache maintenance, the proxy, or another
  external endpoint.

## Static proxy boundary

- `TGVIO_STATIC_PROXY_URL` is an optional deployment environment setting. Its
  value is validated but never placed in a safe summary, database detail,
  Telegram output, or release evidence.
- A real Bot startup performs only a short bounded TCP handshake to the
  configured endpoint. It sends no proxy credentials or remote request and
  records only `reachable` or `unreachable` plus an epoch timestamp.
- Disabled configuration records `disabled`. Offline `--check` records
  `configured_unchecked` instead of opening a socket, so foundation gates
  remain network-free even if a caller inherited a deployment proxy setting.
- Proxy detection failures are non-fatal observability results; they do not
  start dynamic retry, switching, or a coordinator.

## Release identity and schema

The runtime image exposes `RELEASE_ID`, `APP_COMMIT`, and `SOURCE_MANIFEST` as
build-time identity values for the safe snapshot. This candidate adds no
migration and does not change the production v7 schema or any business state
machine.

## Candidate verification

- focused diagnostics/config/UI/release-tooling tests: 91 passed;
- complete Bot-disabled foundation gate: 350 passed in 26.845 seconds;
- source secret/path scan: 185 files passed;
- architecture gate: 72 Python files passed;
- `compileall` and `git diff --check`: passed.

The remaining release steps are deliberately not claimed here: clean commit,
push to `origin/main`, formal Docker `--network none` build gate, HostDZire
preflight/cutover, independent postflight, rollback-asset check, and a
post-release evidence update.
