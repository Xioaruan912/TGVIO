# TGVIO

TGVIO is the clean-room rewrite of `telegram-video-forwarder`.

> Source authority recovery: the runtime in this repository was recovered from
> the HostDZire production TGVIO instance during R2-01 on 2026-09-11. The
> evidence-backed refactoring plan and compatibility contract live in
> [`docs/refactor-v2/`](docs/refactor-v2/README.md). The retired runtime remains
> available through Git history and must not be mixed back into this package.

The product idea stays the same: accept Telegram/media inputs, build a durable
job, decide how the media should be published, publish it to Telegram, and
optionally run secondary transports such as backup storage.

The implementation is intentionally different. TGVIO starts from explicit
domain models, durable state, ports/adapters and a central orchestrator instead
of growing behavior inside one large bot runtime.

## Current phase

TGVIO is now the active production runtime. The current implemented path is:

```text
Telegram media / album
  -> allowlist
  -> durable job
  -> local download
  -> ffprobe/media analysis
  -> PublishPlan v2
  -> side-effect-aware Telegram publish
  -> independent WebDAV Archive lifecycle
```

PublishPlan persistence and the generic side-effect-aware Execution Engine are
implemented. Native Telegram channel/discussion transport, generated video
covers, faststart remux, >2GB playable segmentation, binary volumes and durable
Telegram media-reference reuse are also implemented. Spoiler media is preserved
through explicit Telethon InputMedia objects, including custom TGVIO album
construction so fresh-upload conversion cannot drop the spoiler bit. Video
uploads also get best-effort Telegram thumbnails and media captions use the
persisted channel/group footer policy. Publishing remains protected by the
separate `TGVIO_PUBLISH_ENABLED` gate; the audited HostDZire deployment has
that gate enabled and contains confirmed publish effects.

The controlled fixture path has its own source-safe, disabled-by-default gate
`TGVIO_LIVE_FIXTURE_ENABLED`. The audited production deployment explicitly
enables it. It remains independent from automatic publishing and only exposes
the hidden `/publish <job>` flow with a second confirmation and strict
item/size/strategy limits.

## Bot commands

- `/start` - TGVIO home
- `/status` - runtime and durable job status
- `/jobs` - recent jobs
- `/job [job id]` - correlated Job/Publish/Archive diagnostic view
- `/plan [job id]` - persisted PublishPlan preview
- `/stats` - owner-scoped task/media/event statistics
- `/health` - durable SQLite/runtime/Telegram health
- `/diag` - redacted runtime diagnostics and build commit
- `/diag job <job id>` - deep per-job diagnostics with recent redacted structured log events
- `/retry <job id>` - side-effect-aware retry for failed jobs
- `/cancel <job id>` - durable cancellation at a safe processing boundary
- `/cache` - local cache usage/retention status (`/cache clean` for terminal jobs)
- `/archive` - WebDAV Archive package status, scoped capability probe and explicit retry
- `/help` - usage help

TGVIO resets the retired bot command list on startup so Telegram clients do not
continue showing old `/queue`, `/begin`, `/webdav`, `/dashboard`, etc. entries.

Docker health is not just a SQLite existence check. When the Bot runtime is
enabled, TGVIO writes durable process/Telegram heartbeats and the container
healthcheck requires both to remain fresh and connected. `/status` renders the
same durable Telegram connectivity state.

`/stats`, `/health`, `/job`, and `/diag` are read-only. They never send a Telegram
probe, touch WebDAV, or trigger cache cleanup. Diagnostics only use the static
settings safe-summary plus durable Job/Publish/Archive state, low-cardinality
runtime facts, the image build commit, and already-redacted structured log
metadata. Captions, user ids, source URLs, media paths and credentials are
excluded from diagnostic rendering.

## URL intake

TGVIO can route a plain HTTP(S) link through yt-dlp and then through the same
durable download → analysis → PublishPlan pipeline used by Telegram media. URL
intake is disabled in the source example and does not implicitly enable
Telegram publishing. The audited HostDZire deployment explicitly enables URL
intake.

```text
TGVIO_URL_ENABLED=false
TGVIO_URL_PRIVATE_NETWORK_POLICY=block
```

The default policy blocks directly resolved private/local targets. URLs with
embedded credentials, fragments, or credential-like query parameters are
rejected before persistence. Download output is constrained to the Job's
managed directory and downloader failures do not echo the source URL into the
durable error message.

Telegram media intake also has a short smart batching window. Telegram may
deliver a large user selection as several adjacent albums because one Telegram
media group is limited, but TGVIO treats that transport limit separately from
the logical Job. By default adjacent media/albums from the same owner/chat are
gathered for 1.5 seconds (maximum wait 5 seconds) and up to 100 items are sent
to one PublishPlan. Set `TGVIO_BATCH_WINDOW_MS=0` to restore immediate one-event
Job creation.

## Local cache lifecycle

TGVIO keeps local media long enough for durable retry/recovery instead of
blindly deleting every completed download. Automatic cleanup defaults to 24
hours and only removes cache for `succeeded` or `cancelled` jobs. `planned` and
`failed` jobs are deliberately retained because they may still need publish or
retry recovery. An ArchivePackage that has not reached `committed` or
`cancelled` also blocks deletion so canonical media cannot disappear mid-archive.

```text
TGVIO_CACHE_RETENTION_HOURS=24
TGVIO_CACHE_CLEANUP_INTERVAL_MINUTES=30
```

`/cache` shows managed usage and cleanup eligibility. `/cache clean` skips the
age window for terminal jobs but still refuses to delete files required by an
ArchivePackage that has not reached `committed`/`cancelled`.

Active jobs expose durable phase progress in `/jobs`. Telegram and yt-dlp
downloads report byte counters while analysis/publish report item/step
counters. Progress rows contain only phase/index/count/byte values and do not
store filenames, captions, media URLs or credentials.

## Structured operational logs

TGVIO writes one JSON object per log line to stdout and, by default, to the
persistent `logs/tgvio.jsonl` volume. File logs rotate independently from the
Docker `json-file` driver so container recreation does not erase the primary
troubleshooting history and neither sink grows without a bound.

Important pipeline transitions have stable event names and correlation fields,
for example `job_id`, `plan_id`, `package_id`, `step_index`, `object_index`,
`error_code`, byte counts and elapsed milliseconds. Typical event families are
`intake.*`, `download.*`, `analysis.*`, `publish.*`, `archive.*`, and
`runtime.*`. Filenames, captions, configured URLs, credentials, authentication
headers and managed media paths are not intentional log fields; the formatter
also redacts URLs, common secret assignments and managed runtime paths from
free-form messages and exception text.

Useful production queries can be run without parsing Docker's human output:

```text
sh scripts/logs.sh --tail 100
sh scripts/logs.sh --job <full-job-id> --tail 200
sh scripts/logs.sh --event publish. --level ERROR --tail 100
sh scripts/logs.sh --package <archive-package-id> --tail 200
```

Logging configuration:

```text
TGVIO_LOG_LEVEL=INFO
TGVIO_LOG_DIR=/app/logs
TGVIO_LOG_FILE_ENABLED=true
TGVIO_LOG_MAX_MB=20
TGVIO_LOG_BACKUP_COUNT=5
```

## WebDAV Archive V2

WebDAV is implemented as a durable archive system rather than an upload-attempt
queue. One Job maps to one stable ArchivePackage using a human-readable layout:

```text
archive/YYYY/MM/DD/<package>/
  media/001__original.ext
  media/002__original.ext
  manifest.json
  _COMPLETE.json
```

The planner archives canonical media only. Telegram cover frames, thumbnails,
temporary remuxes and split transport parts are deliberately excluded. V2
state lives in `archive_packages`, `archive_objects`, and `archive_events`.
The V2 executor is now implemented. It starts capability discovery with
OPTIONS/PROPFIND, validates canonical files against their planned SHA-256, resumes stored
objects after restart, verifies remote size, writes `manifest.json`, and writes
`_COMPLETE.json` only after final verification. Servers that advertise MOVE
use `.staging/<package>` plus collection MOVE; other servers use the final
directory directly with the complete marker as the commit boundary.

The Archive runtime is wired into the Bot lifecycle. Source defaults remain
disabled; production is explicitly enabled after endpoint validation. When enabled it plans the package without network I/O, then a separate
worker executes it. A failed package does not mutate Telegram Job state and is
not retried in a tight automatic loop; `/archive retry <job id>` explicitly
returns that same durable package to staging after validating local canonical
cache. `/archive probe` begins with OPTIONS/PROPFIND. If the DAV frontend omits
write methods from `Allow`, TGVIO performs one tiny isolated
`.staging/.capability-*` write/read/move fixture under the configured archive
root and deletes it immediately; the result is cached for the process lifetime.

The old attempt-centric runtime/code has been removed. Existing production
databases may still contain historical `backup_attempts`/`backup_files` tables;
they are intentionally left untouched for non-destructive compatibility and
are no longer read by TGVIO.

Configuration remains outside SQLite and Git and uses Archive V2 names only:

```text
TGVIO_ARCHIVE_ENABLED=false  # source-safe default; production explicitly enables it
TGVIO_ARCHIVE_WEBDAV_URL=
TGVIO_ARCHIVE_REMOTE_ROOT=TGVIO
TGVIO_ARCHIVE_WEBDAV_USER=
TGVIO_ARCHIVE_WEBDAV_PASSWORD=
TGVIO_ARCHIVE_POLL_SECONDS=10
```

Credentials and the full WebDAV URL are never included in safe summaries,
archive events, manifests, or user-facing status output.

Production Archive V2 has passed real endpoint validation: scoped capability
probing, staging + MOVE commit, canonical media verification, `manifest.json`,
`_COMPLETE.json`, live background-runtime processing, and fixture cleanup all
succeeded. Production therefore runs with `TGVIO_ARCHIVE_ENABLED=true` while
Telegram auto-publish remains controlled by its independent explicit gate.

## Safety rule

`TGVIO_RUN_BOT=false` remains the safe source default. Production explicitly
sets it to true. `TGVIO_PUBLISH_ENABLED=false` remains the safe source default
for externally visible channel publishing; the audited production deployment
explicitly sets it to true. Secrets remain outside Git and Docker build
context.

## Layout

```text
src/tgvio/
  domain/          Pure business concepts
  application/     Use-cases and orchestration
  adapters/        Telegram and future external adapters
  infrastructure/  SQLite, filesystem, FFmpeg, network implementations
  config.py        Typed environment contract
  main.py          Composition root only
```

## Local checks

```bash
python -m tgvio.main --check
python -m unittest discover -s tests -v
```

## Deployment

Deployment credentials are never copied into Git. The production target is
`/root/TGVIO` and the single Compose service/container is `tgvio`. Every tested
release must follow the backup, single-instance cutover, verification and
rollback protocol in
[`DEPLOYMENT_HOSTDZIRE.md`](docs/refactor-v2/DEPLOYMENT_HOSTDZIRE.md).
