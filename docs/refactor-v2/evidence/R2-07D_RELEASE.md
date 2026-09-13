# R2-07D Diagnostic Snapshot Release

> Date: 2026-09-13
> Git runtime commit: `10b6dd5f58f8839a6fa51428a32b93d8f95c098c`
> Release: `r2-07d-10b6dd5-20260913T091106Z`
> Migration: `none`
> Schema: v7 / `9cf2d4008d4fb888f5affdffea1ccd413b51968d14d234e07d0769cfe60c6e90`

## Delivered behavior

R2-07D adds a fixed, local-only, redacted `DiagnosticSnapshot` and a
deployment-only static-proxy reachability status. It completes the retained
R2-07 scope; dynamic Destination Profiles and dynamic Proxy Profiles remain
retired.

- The snapshot domain DTO exposes only an allowlist: release identity
  (`RELEASE_ID`/`APP_COMMIT`/`SOURCE_MANIFEST`), schema/migration verification,
  runtime-lease summary, scheduler aggregates, Archive aggregates and
  capability freshness, non-sensitive feature flags, and static-proxy
  state/last-check.
- Scheduler, Archive, and runtime-lease values come from three bounded SQLite
  aggregate queries. The snapshot never loads Job history, media records,
  captions, peer/user identifiers, or paths.
- Runtime-health details are never rendered wholesale. The proxy projection
  accepts only its enum state and a bounded epoch timestamp; invalid or
  oversized values fall back to safe defaults.
- Release identity values are regex-validated before rendering; exceptions are
  normalized to component availability states instead of passing through
  exception text.
- Bare `/diag` and the mobile “技术诊断” button render only the snapshot
  service. They never call Telegram, WebDAV, cache maintenance, the proxy, or
  any other external endpoint.
- `TGVIO_STATIC_PROXY_URL` is an optional deployment setting. On a real Bot
  startup TGVIO performs one short bounded TCP handshake and records only
  `reachable`/`unreachable` plus an epoch; credentials are never sent and the
  endpoint is never retained in the result. Offline `--check` records
  `configured_unchecked` and opens no socket. Proxy detection never enables
  dynamic switching or a coordinator.

## Changes

- New `domain/diagnostics.py`, `application/diagnostics.py`, and
  `infrastructure/proxy_probe.py`.
- `config.py` adds `TGVIO_STATIC_PROXY_URL` (with `repr=False`) and
  `TGVIO_STATIC_PROXY_PROBE_TIMEOUT_SECONDS` plus validation.
- `ports.py` adds `get_diagnostic_aggregates`; `sqlite_observability.py`
  implements it with three bounded aggregate queries.
- `main.py` records a redacted `static_proxy` runtime-health row at startup and
  injects the snapshot service into the Bot UI.
- `adapters/telegram/bot_ui.py` renders the fixed snapshot for `/diag`.
- `Dockerfile` now exports `RELEASE_ID` and `SOURCE_MANIFEST` as build-time
  identity env values; the release tooling already passes all four build args.
- Release-ID validators across `deploy_hostdzire.py`, `remote_release.sh`, and
  `rollback_hostdzire.sh` accept an `R2-NN` work-package suffix such as
  `R2-07D`.

## Migration

The release declared `none`. No schema change was made: production remains
`PRAGMA user_version=7` with normalized schema SQL SHA-256
`9cf2d4008d4fb888f5affdffea1ccd413b51968d14d234e07d0769cfe60c6e90`.

## Verification before cutover

- close-out gates: forbidden-path/credential scan 185 files passed; architecture
  gate 72 Python files passed; `git diff --check` passed; `compileall` passed.
- local clean diagnostic Docker build (`scripts/build_check.sh`, `--network none`):
  test target **350 tests**, runtime image inspection passed; source manifest
  `5d29d0b9d6a685eb8b3e98fbbc1b62be574ac55b01f9412e28f77f8f892c4333`.
- The formal clean/pushed Docker test target on HostDZire ran `--network none`
  with **350 tests** and built release image
  `sha256:0b88a34b1055015f2397b164c0ee9885c1cf5fe3cf9552c501f54e4c35051333`.
- No second production Bot or production Telegram session was used.

## Production preflight

Immediately before cutover the existing R2-07A3 production state was
independently read-only checked:

- release `r2-07a3-98e2aaa-20260913T072332Z`;
- commit `98e2aaac4598315b23e8224d4e6c888db155aef0`;
- source manifest `37cac7ac97c6ad1bbfc2403e0f3719ecc33b0b2016de2afd51da49a3fd1421a0`;
- SQLite `user_version=7`, schema hash unchanged, `quick_check=ok`;
- 24 Jobs; all business blockers 0; one active runtime lease;
- one healthy container, restart count 0; `safe_to_deploy=true`.

The HostDZire report continued to show `ntp_synchronized=no`; this remains a
known non-blocking host maintenance item.

## Production cutover

Formal deployment used only the approved entry point:

`python3 scripts/deploy_hostdzire.py --phase R2-07D`

Release identity:

- release: `r2-07d-10b6dd5-20260913T091106Z`;
- commit: `10b6dd5f58f8839a6fa51428a32b93d8f95c098c`;
- source manifest: `5d29d0b9d6a685eb8b3e98fbbc1b62be574ac55b01f9412e28f77f8f892c4333`;
- runtime image: `sha256:0b88a34b1055015f2397b164c0ee9885c1cf5fe3cf9552c501f54e4c35051333`;
- release test count: 350.

The path created the standard SQLite/source/.env/image triple rollback points,
performed the single-instance recreate, and wrote the deployment ledger. No
proxy endpoint was configured, so the snapshot reported `disabled`.

## Rollback assets

Ledger `r2-07d-10b6dd5-20260913T091106Z/evidence/deployment-ledger.json`:

- database: `/root/TGVIO-releases/r2-07d-10b6dd5-20260913T091106Z/rollback/state-pre.sqlite3`
  (SHA-256 `8abed3970a100132a187cce1f856d2ee58a0eabe79cd133985fe2e3ce6b551ad`, `quick_check=ok`, 24 Jobs, v7);
- source: `/root/TGVIO-releases/r2-07d-10b6dd5-20260913T091106Z/rollback/source-pre.tar.gz`
  (SHA-256 `bae4f17408093734d926ed632d76fdc02cc10f4ce9d996c126ac4830bdc9cb71`);
- environment: `/root/TGVIO-releases/r2-07d-10b6dd5-20260913T091106Z/rollback/env-pre.bak` (mode 600);
- image tag: `tgvio-rollback-pre:r2-07d-10b6dd5-20260913T091106Z` (previous image
  `sha256:c432d3aaf8314214a37a321c5e58a43a21e0fe728f89cb1b994d70d40aad7eee`, previous release `r2-07a3-98e2aaa-20260913T072332Z`).

## Independent postflight

`scripts/vps_check.sh` after cutover confirmed:

- running `APP_COMMIT` and release commit exactly `10b6dd5f58f8839a6fa51428a32b93d8f95c098c`;
- source manifest exactly `5d29d0b9d6a685eb8b3e98fbbc1b62be574ac55b01f9412e28f77f8f892c4333`;
- runtime image exactly `sha256:0b88a34b1055015f2397b164c0ee9885c1cf5fe3cf9552c501f54e4c35051333`;
- one running instance, Docker health `healthy`, restart count 0;
- bootstrap marker 1, Telegram-ready marker 1, error marker 0;
- 24 Jobs, all business blockers 0, one active runtime lease;
- `quick_check=ok`, `PRAGMA user_version=7`, schema hash unchanged;
- `safe_to_deploy=true`.

A separate read-only check confirmed the running container exposes the real
identity env (`RELEASE_ID`, `SOURCE_MANIFEST`, `APP_COMMIT`) and durable
`runtime_health` rows:

- `schema=ready` with ledger `1..7`;
- `static_proxy=disabled` (`{"version": 1}`) because no proxy is configured;
- `telegram=connected`; `runtime=alive`.

Rollback asset validation passed:

`bash scripts/rollback_hostdzire.sh --check r2-07d-10b6dd5-20260913T091106Z`

## Safety conclusion

R2-07D is production-delivered. The retained R2-07 scope is complete: single
Archive profile/policy, durable Archive recovery/status, exact remote Archive
delete, and the fixed redacted Diagnostic Snapshot with deployment-only static
proxy status. Dynamic Destination Profiles and dynamic Proxy Profiles remain
explicitly retired.
