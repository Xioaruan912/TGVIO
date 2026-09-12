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

TGVIO is now the active production runtime. R2-01 restored source authority and
R2-02 established the reproducible, fail-closed delivery chain. Production
release `r2-02-569926b-20260911T063133Z` at full commit
`569926b53af19539b118daa93f95c58da2001637` is healthy on HostDZire. The current
implemented path is:

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

## Bot controls

Run `/start` once to install a persistent mobile keyboard with six compact
entries: home, my jobs, status, Archive, cache, and more. Recent jobs have
direct detail buttons. Job detail provides context-sensitive plan, safe retry,
cancel, Archive retry, and redacted technical-detail buttons, so normal use
never requires copying a job ID. Mutating/network actions opened from buttons
have a separate confirmation page.

The Telegram command menu intentionally exposes only `/start`, `/jobs`,
`/status`, and `/help`. Existing advanced commands remain accepted for
backward compatibility, but they are no longer presented as the normal mobile
workflow. TGVIO resets legacy command scopes on startup so clients do not keep
showing the retired long menu.

Docker health is not just a SQLite existence check. When the Bot runtime is
enabled, TGVIO writes durable process/Telegram heartbeats and the container
healthcheck requires both to remain fresh and connected. `/status` renders the
same durable Telegram connectivity state.

Statistics, health, job detail, and diagnostics are read-only. They never send a Telegram
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

Known-size Telegram files use bounded concurrent range downloads for throughput.
If all retries for a concurrent shard are exhausted, TGVIO removes the partial
file and makes one automatic single-stream attempt before failing the Job.
Explicit cancellation never starts this fallback, and every successful result
still passes the same size check and atomic rename boundary.

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

The cache page shows managed usage and cleanup eligibility. Its confirmed
cleanup action skips the age window for terminal jobs but still refuses to
delete files required by an ArchivePackage that has not reached
`committed`/`cancelled`.

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
not retried in a tight automatic loop; the confirmed Archive retry button
returns that same durable package to staging after validating local canonical
cache. The confirmed connection test begins with OPTIONS/PROPFIND. If the DAV frontend omits
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
sh scripts/check_foundation.sh
```

For a clean, pushed checkout, the isolated Docker diagnostic build is:

```bash
scripts/build_check.sh
```

It runs the test image with `--network none`, inspects the minimal runtime
image, never reads production configuration and never starts the Bot. It is a
test build, not a production release.

## Deployment

Deployment credentials are never copied into Git. The production target is
`/root/TGVIO` and the single Compose service/container is `tgvio`. Every tested
release must follow the backup, single-instance cutover, verification and
rollback protocol in
[`DEPLOYMENT_HOSTDZIRE.md`](docs/refactor-v2/DEPLOYMENT_HOSTDZIRE.md).

The formal release entrypoint proven by R2-02 is:

```bash
python3 scripts/deploy_hostdzire.py --phase R2-02
```

It accepts only a clean full commit already present at live `origin/main`,
builds and tests the candidate on HostDZire without production mounts or
network, creates three rollback points, recreates the sole service once, and
writes a non-secret release manifest. Operational details are in
[`RELEASE_TOOLING.md`](docs/refactor-v2/RELEASE_TOOLING.md); the first production
acceptance record is
[`R2-02_RELEASE.md`](docs/refactor-v2/evidence/R2-02_RELEASE.md). The next planned
stage is R2-03 migration-ledger takeover; schema changes remain prohibited until
its production-copy rehearsal passes. Each later code stage must update the
explicit `--phase` only after its own release gates support that stage.
