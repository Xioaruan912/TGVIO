# R2-11 Owner Alerts and Collection Preview Release

> Date: 2026-09-13
> Git runtime commit: `7101d2a5c660ddd64cf08f41c68f8be650a6e291`
> Release: `r2-11-7101d2a-20260913T122158Z`
> Migration: `none`
> Schema: v8 / `5b80e9d9aa0abe258883e5eb8b26d54ce2481b4e03daa3ee45f75e477423f76a`

## Delivered behavior

R2-11 adds two product improvements on top of the R2 refactor. No schema,
callback-contract, or state-machine change.

### Owner failure alerts (default on)

- New `TelegramOwnerNotifier` delivers a redacted alert to the owner's private
  Telegram chat; the outbox claim/lease/backoff/dead-letter semantics are reused.
- Notification delivery is now primary/secondary: the Telegram owner channel only
  receives failures/anomalies, while an optional webhook (if enabled) receives
  the full whitelisted stream. A webhook outage is best-effort and never blocks
  the owner alert.
- Alert scope: `job.failed` (including partial/uncertain), `archive.failed`, and
  new runtime conditions `runtime.telegram_disconnected` / `runtime.disk_low`
  plus their recovery events.
- `AlertRuntime` polls `telegram` health and download-root free space; alerts are
  transition-triggered with a cooldown (`TGVIO_ALERT_COOLDOWN_SECONDS`, default
  3600) to avoid spam.
- Redaction: alerts contain only `任务 #N`/state/error code/component/bytes/time,
  never captions, URLs, paths, or credentials.
- Config: `TGVIO_ALERTS_ENABLED=true`, `TGVIO_ALERT_USER_ID=` (empty → first
  `ALLOWED_USERS`), `TGVIO_ALERT_COOLDOWN_SECONDS=3600`,
  `TGVIO_ALERT_POLL_SECONDS=60`.

### Collection preview (default on)

- `/end` (button or command) now shows a preview card instead of immediately
  enqueuing: media counts by kind, total size, cover plan, discussion groups,
  caption lines/chars, and the current spoiler mode.
- Buttons: `[✅ 确认发布] [🔞 显示模式]` and `[❌ 放弃]`. Only the confirm action
  finalizes the collection and creates Jobs; abandon cancels it. Nothing is
  downloaded before confirmation.
- Config: `TGVIO_COLLECTION_PREVIEW_ENABLED=true`; when disabled, `/end`
  finalizes immediately as before.
- Callback data stays within the 64-byte limit (`intake:confirm:<session_id>`).

## Verification before cutover

- close-out gates: forbidden-path/credential scan 222 files passed; architecture
  gate 100 Python files passed with the 1000-line budget; `git diff --check` and
  `compileall` passed.
- local clean diagnostic Docker build (`--network none`): **380 tests**; source
  manifest `932cad1e7df3a2a76098f214d73188ed6ecfa4323131c5f5d52951dc44c44c8f`.
- formal HostDZire test target under `--network none`: **380 tests**.

## Production cutover (fail-closed, retried after active Archive)

The first formal invocation correctly **fail-closed** before any production
mutation: the control preflight reported `database-activity-or-integrity`
because a 2.5 GB Archive package was actively uploading. No cutover happened.
After that Archive attempt ended and the durable claim cleared (`blockers=[]`,
`safe_to_deploy=true`), the release was retried and completed:

- release: `r2-11-7101d2a-20260913T122158Z`;
- commit: `7101d2a5c660ddd64cf08f41c68f8be650a6e291`;
- source manifest: `932cad1e7df3a2a76098f214d73188ed6ecfa4323131c5f5d52951dc44c44c8f`;
- runtime image: `sha256:c78f3eb4992192281c79a7243eb4926573acb4436cdc72fa4dddd126cbe63d79`;
- release test count: 380.

No schema change; production remains `user_version=8`.

## Rollback assets

- database: `/root/TGVIO-releases/r2-11-7101d2a-20260913T122158Z/rollback/state-pre.sqlite3`
  (SHA-256 `03b84b3bb356528b8c267fe72e673d2feb825f31d9ffdf261cb7d78cde3a6728`, v8);
- source: `/root/TGVIO-releases/r2-11-7101d2a-20260913T122158Z/rollback/source-pre.tar.gz`
  (SHA-256 `90a217f0d952cda803a6ced8d093e6c68c2e3f0e41298bc6362c531d5cda85fc`);
- environment: `.../rollback/env-pre.bak` (mode 600);
- image tag: `tgvio-rollback-pre:r2-11-7101d2a-20260913T122158Z`
  (previous release `r2-08b-52f39ca-20260913T101837Z`, image
  `sha256:507d38f97c3d5d3d60d2d2295ec2c5e850d053554e70cc6af73d9ea6bdf3bd0e`).

## Independent postflight

`scripts/vps_check.sh` after cutover confirmed one healthy single instance,
restart count 0, `blockers=[]`, `safe_to_deploy=true`, `quick_check=ok`,
`user_version=8` with unchanged schema hash, and 24 Jobs with all business
blockers 0. `runtime_health` showed `telegram=connected`, `schema=ready`,
`runtime=alive`, `static_proxy=disabled`; `notification_outbox` had no pending
rows. No alert/traceback markers were emitted. Rollback asset validation passed:

`bash scripts/rollback_hostdzire.sh --check r2-11-7101d2a-20260913T122158Z`

## Note

A pre-existing 2.5 GB Archive package (`arc_4334612…`) was manually retried and
failed again during the release window; failed packages are not deploy blockers
and did not affect the cutover. This remains a WebDAV-side backup failure for the
owner to review from `/jobs` / the Archive view.
