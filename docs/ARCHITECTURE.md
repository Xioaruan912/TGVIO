# TGVIO Architecture Notes

## Why rewrite instead of continue refactoring

The old project proved the product and accumulated important edge-case
knowledge, but transport state, UI state, durable state and publishing policy
grew too close together. TGVIO keeps the proven behavior while changing the
control model.

## Core flow

```text
Telegram / URL / future input
        |
        v
     Intake
        |
        v
 Durable Job ----> Media Analysis
        |               |
        +-------> Planner / Orchestrator
                        |
                 immutable PublishPlan
                        |
                 Execution Engine
                  /            \
             Telegram       Archive runtime
                  \            /
                   durable events
```

## The important change: plan first, execute second

The old runtime often decides the next action while already executing the job.
TGVIO should first produce a `PublishPlan` that answers questions such as:

- which photos are channel cover presentation slots;
- which photos overflow to comments;
- which videos/documents belong to which media group;
- whether an item can reuse an existing Telegram reference;
- whether retry is safe after a partial side effect;
- which canonical media facts may later feed the independent ArchivePlan.

The plan is persisted before execution. A restart therefore does not need to
reconstruct business intent from scattered in-memory dictionaries.

## Durable state

SQLite is authoritative from the first version. In-memory structures may speed
up runtime work but must never be the only copy of information required for
retry, resume, cancellation or UI rendering.

Recommended future tables:

- `jobs`
- `job_items`
- `job_events`
- `publish_plans`
- `publish_steps`
- `publish_effects`
- `archive_packages`
- `archive_objects`
- `archive_events`
- `telegram_file_cache`

Phase 1A now implements the first three as normalized tables. Media metadata is
stored per item instead of embedding the whole collection in one job JSON
blob. State transitions are validated centrally and every accepted transition
appends a durable event in the same transaction.

Phase 2 implements `publish_plans`, `publish_steps`, and `publish_effects`.
Each plan is persisted before execution and contains exact media indexes,
channel/discussion target, step kind, and per-item strategy. Confirmed external
side effects are journaled separately from plan intent.

Current lifecycle:

```text
received
  -> downloading -> downloaded
  -> downloaded

downloaded -> analyzing -> analyzed -> planned -> publishing -> succeeded

Any non-terminal pre-publish phase may fail or be cancelled. Publishing may
succeed or fail. Terminal states do not transition further.
```

## Smart behavior without an LLM dependency

"Smart" initially means deterministic planning based on facts:

- media kind and count;
- file size;
- codec/container compatibility;
- Telegram album constraints;
- cover/display policy;
- destination capabilities;
- known Telegram file references;
- previous confirmed side effects;
- disk/network capacity.

An LLM can later become an optional intent adapter for natural-language
commands, but it should not be required for correct media execution.

## Ideas worth borrowing from other projects

### mirror-leech style bots

Borrow:

- explicit task/status concepts;
- incomplete-task recovery;
- multiple download/upload backends;
- user-facing progress and cancellation.

Avoid:

- large global runtime state;
- feature flags spread across handlers;
- direct coupling between command parsing and transport execution.

### worker-separated downloader bots

Borrow the idea that receiver and heavy worker can become separate processes
later. Do not add Redis on day one. Start with SQLite + one process and keep the
port boundary clean enough that a Redis/NATS worker can be introduced only when
there is a measured need.

### whitelist/cache focused bots

Borrow:

- repository abstraction around SQLite;
- bounded cache and cleanup policies;
- tests that do not require Telegram/network access;
- Telegram file-id/reference reuse as a first-class cache.

## Delivery phases

1. Phase 0 - foundation. **Implemented.**
2. Phase 1 - intake + durable job lifecycle + media analysis. **Implemented.**
3. Phase 2 - PublishPlan v2 + production Bot UI. **Implemented.**
4. Phase 3 - Telegram execution + side-effect checkpoints. **Implemented.**
   All PublishPlan v2 strategies have concrete transport paths behind an
   explicit auto-publish gate; the audited production deployment enables it.
5. Phase 4 - retry/recovery/cancel semantics across publish steps. **Implemented.**
6. Phase 5 - WebDAV Archive V2. **Implemented and production-enabled after explicit endpoint validation.**
7. Phase 6 - richer observability and operational controls. **Implemented for durable health/progress, structured operational logging and correlated per-Job diagnostics; richer alerts remain future work.**

Phase 6 includes a correlated per-Job diagnostic read model. `/job <id>` joins
durable Job events, current progress, PublishPlan/steps/effects and the
independent ArchivePackage/object state, then emits deterministic recovery
hints for cases such as `publish_partial`, `publish_uncertain`, disk guards,
analysis failures and Archive failure. `/diag job <id>` adds a bounded tail of
already-redacted structured JSONL events from the current and rotated log
files. This diagnostic path is read-only and does not probe Telegram/WebDAV or
render captions, source URLs, credentials or local media paths.

URL intake now uses the same application boundary rather than a second ad-hoc
pipeline. Telegram text is mapped to a durable URL MediaItem only when the URL
gate is enabled and syntax passes the pre-persistence safety check. A routed
MediaDownloader selects the yt-dlp adapter by durable `source_type`; after the
download, the existing FFprobe analyzer reclassifies the placeholder document
into photo/video/document facts before planning.

The Telegram adapter also separates Telegram's album transport boundary from
the TGVIO logical Job boundary. A small same-owner/same-chat debounce window
combines adjacent single-media/Album events before `IntakeService.accept()`.
This lets Planner v2 reason over the full burst (for example 25 photos + 27
videos) while still capping one Job at a configured item count. Pending gather
batches are flushed during graceful shutdown.

The JobDownloader also polls the durable cancel flag while an individual
download is in flight. Download cancellation is safe to interrupt because its
only side effect is a managed local temporary file; Telegram publishing keeps
the stricter rule of never cancelling a possibly-visible send request.

Local cache has a separate maintenance lifecycle. Automatic cleanup only
considers `succeeded` and `cancelled` jobs after the configured retention
window. `planned` and `failed` remain untouched because their local files are
part of the durable publish/retry recovery contract. When a Job has an Archive
package, any package that is not `committed`/`cancelled` also vetoes deletion.

Operational reads stay side-effect free. `/stats` aggregates only owner-scoped
job/item counts and recent event *types*; `/health` consumes local SQLite and
durable heartbeat state; bare `/diag` renders a fixed `DiagnosticSnapshot`
allowlist (release identity, migration/lease/scheduler/Archive aggregates,
feature flags, and redacted static-proxy state). The snapshot never renders
runtime-health details wholesale, endpoints, credentials, captions, IDs, or
paths. Existing owner-scoped `/diag job <id>` remains a separate bounded task
diagnostic path. None of these routes performs Telegram send probes, WebDAV
requests, cache deletion, or external proxy probes.

When `TGVIO_DASHBOARD_ENABLED=true`, a separate loopback-only read-only HTTP
adapter serves an anonymous HTML shell plus Bearer-protected JSON DTOs
(`/api/v1/overview|jobs|routing|storage|health`) and low-cardinality Prometheus
`/metrics`. Only GET/HEAD are accepted; query tokens, request bodies and
oversized headers are rejected; every response carries strict security headers.
The DTOs exclude owners, peers, captions, URLs, paths and credentials. When
`TGVIO_WEBHOOK_ENABLED=true`, the durable `notification_outbox` idempotently
mirrors terminal Job/Archive events and dispatches them over HTTPS with an
HMAC-SHA256 signature and bounded exponential retry; payloads use a fixed
allowlist and never include captions, identifiers or credentials. Both surfaces
are disabled by default and never run a second Telegram session.

`job_progress` is a small durable read model for UI progress. The downloader
adapters emit byte callbacks into `JobDownloader`, which throttles writes; the
analyzer and execution engine checkpoint item/step counters. This survives Bot
UI refresh/restart without putting transport objects or sensitive media labels
into SQLite.

Production cutover already happened on 2026-09-03. Future phases extend TGVIO
in place; the retired project is kept only as a rollback/reference archive.

## Implemented foundation status

Phase 1A is implemented with normalized `jobs`, `job_items`, and `job_events`
tables plus guarded durable state transitions.

Phase 1B now includes a deterministic media inspector. It records filesystem
facts, SHA-256, MIME guess, ffprobe container/codec/dimensions/duration, basic
MP4/MOV faststart detection, album eligibility, Telegram streamability
candidates, document-send candidates, and large-file flags. Analysis does not
publish anything; it only enriches durable media facts for the planner.

Phase 1C wires the first real production intake path:

```text
Telegram message / album
        -> allowlist check
        -> durable Job + MediaItems
        -> Telegram downloader
        -> downloaded
        -> Media Analyzer
        -> analyzed
        -> PublishPlan v2
        -> planned
```

The downloader reconstructs Telegram source messages from durable
`source_chat_id` + `source_message_id` values, so no Telethon object is needed
to survive a process restart. Files are written through a temporary `.part`
path and atomically renamed after completion. A configurable disk reserve is
checked before each transfer. The source-safe default stops after planning
unless `TGVIO_PUBLISH_ENABLED=true`; the audited production deployment enables
the gate and contains confirmed publish effects.

At startup, pre-publish jobs in `received`, `downloading`, `downloaded`, or
`analyzing` are scheduled back through the same ingestion processor. Completed
local files are reused, analysis is deterministic and repeatable, and no
Telegram publishing side effect exists in this recovery path yet.

Non-media documents such as PDF/ZIP are not forced through ffprobe. They still
receive filesystem size, MIME inference, SHA-256 and document-send planning
facts; media-looking documents continue through ffprobe normally.

The production Bot UI is also owned by TGVIO. Startup resets legacy Bot command
definitions and installs the TGVIO command surface including `/plan`, `/stats`,
`/health`, `/diag`, `/cache`, and `/archive`. `/plan` is a read-only rendering
of persisted publish intent; `/archive probe` is a read-only WebDAV capability
probe and creates no remote test file.

Phase 3 now includes a native Telegram transport for channel albums,
discussion-thread replies, documents, generated video covers and faststart
remux. The adapter receives prior durable effects from the Execution Engine,
so it resolves discussion roots from confirmed channel messages instead of
holding thread ids only in memory.

Bot accounts cannot call MTProto `messages.getDiscussionMessage`. Discussion
resolution therefore uses a bot-safe adapter: Bot API `getChat` discovers the
channel's `linked_chat_id`, then the linked group's pinned automatic-forward is
accepted only when its source channel message id matches one of the confirmed
channel receipts for the current step. The resulting discussion chat/message
ids are copied into that durable channel effect. A restart can therefore reply
to the same thread without repeating the channel post or calling a user-only
method. Channel root send/capture is serialized in the shared publish adapter
so concurrent TGVIO jobs cannot race the linked group's single current pinned
automatic-forward observation.

Restart recovery is deliberately conservative. Completed steps are skipped.
Each completed transport call writes all external receipts and a
`publish_step_receipts_committed` marker in one SQLite transaction. A `running`
step with that marker can be reconciled as succeeded without replay. Missing
effects become `publish_uncertain`; effects without the completion marker become
`publish_partial`; neither condition is automatically resent.

Large-file execution is now explicit. `split_playable` produces independently
probeable MP4 segments bounded by `TGVIO_UPLOAD_PART_MB` and a manifest with
SHA-256 checksums. `binary_volume` preserves every input byte in bounded
document volumes and writes a reassembly/checksum manifest. Sequential
manifest/part sends surface already-confirmed receipts through a partial-error
contract if a later send fails, so those visible Telegram messages are not
forgotten.

Telegram media reuse is also durable. `telegram_file_cache` stores an opaque
`telegram:<chat_id>:<message_id>` reference keyed by SHA-256, destination and
media kind. The post-analysis enrichment stage attaches cache hits to
`MediaItem.telegram_ref`, so the Planner selects `reuse_reference` before more
expensive remux/split strategies. The Telethon adapter resolves that message
back to InputMedia; if it has been deleted or can no longer be resolved, the
same step safely falls back to the local file before sending anything. Newly
confirmed native sends refresh the cache.

Spoiler publishing uses explicit Telethon InputMedia objects instead of an
unverified high-level keyword. Local photos/videos/documents are uploaded first
and wrapped with the spoiler bit; reused InputMedia references preserve it as
well. TGVIO also owns album construction because Telethon 1.44's high-level
album helper drops the spoiler bit when it converts fresh uploads through
`UploadMediaRequest`. Fresh album media is explicitly converted to reusable
InputMedia while preserving spoiler; already-existing InputMedia references
bypass `UploadMediaRequest`, avoiding the historical `MediaInvalidError` class
of bug. Generated channel cover frames and split manifests intentionally remain
unspoilered, while the original video or playable split segments keep the
source spoiler semantics.

Caption presentation is a planning policy, not a transport-side global. Each
step carries `forward_caption` and `caption_footer`. A missing `CHANNEL_AT`
falls back to a username-form `DEST_CHANNEL`; `GROUP_AT` remains optional. The
footer is appended even when original caption forwarding is disabled, and
captions are bounded to Telegram's 1024-character media-caption limit.

Video upload thumbnails are generated independently from channel cover images.
The transformer samples 10/20/30 percent, rejects black frames, scales within
320px and progressively lowers JPEG quality until it is <=40 KiB. Failure to
find a suitable thumbnail is an optimization failure only and does not fail the
publish step.

At this point every strategy emitted by PublishPlan v2 has a concrete Telegram
transport path. `TGVIO_PUBLISH_ENABLED` remains the final hard gate and must be
an explicit deployment decision; it is enabled on the audited production
instance.

A separate `TGVIO_LIVE_FIXTURE_ENABLED` gate exists for that validation. It is
off by default and does not enable automatic publishing. If explicitly turned
on, an authorized user can request `/publish <planned-job>`, receive a second
confirmation button, and only then execute a small fixture (<=10 items and
within the configured size ceiling). Large-file split/binary-volume plans are
excluded from this controlled fixture path.

Phase 4 adds durable operator control without reintroducing in-memory retry
tickets. `job_controls` stores cancellation requests and retry counters. The
download, analysis and publish loops check cancellation at safe boundaries;
an in-flight Telegram send is never force-cancelled because doing so could make
the external side effect unknowable. Failed pre-publish jobs may be reset to
`received`, where complete cached files are reused. A failed publish step may
be reset to `planned` only when its own durable effects prove that no visible
message was confirmed. `publish_partial` and `publish_uncertain` remain manual
review states and `/retry` refuses them.

The Telegram adapter now distinguishes pre-side-effect failures from visible
send uncertainty. Exceptions from `send_file` or `SendMultiMediaRequest` are
classified as `publish_uncertain` because Telegram may have accepted the send
even if the response was lost. Transform, validation and target-resolution
failures that occur before any visible send remain `publish_failed` and can be
considered by the safe retry service.

Archive V2 treats WebDAV as a durable archive system, not an upload queue. A
network-free `ArchivePlanner` first materializes one `ArchivePackage` per Job
with ordered canonical `ArchiveObject`s. SQLite stores
`archive_packages/archive_objects/archive_events`; retry continues that same
package rather than creating a second attempt identity.

Remote Layout v1 is deliberately human-readable and self-describing:

```text
archive/YYYY/MM/DD/YYYYMMDD-HHMMSS__media-NNN__<job>/
├── media/
│   ├── 001__original-name.ext
│   └── ...
├── manifest.json
└── _COMPLETE.json
```

Archive execution begins with `OPTIONS` + `PROPFIND` capability discovery.
`PROPFIND`, `MKCOL`, and `PUT` are mandatory; MOVE is optional. Some DAV
frontends expose an incomplete root `Allow` header. When required methods are
omitted, TGVIO performs one tiny isolated `.staging/.capability-*` fixture under
the configured archive root to verify MKCOL/PUT/GET/MOVE, deletes it
immediately, and caches the result for the process lifetime. Servers
that explicitly advertise MOVE upload and verify under `.staging/<package_id>`
then move the complete collection into the final layout. Servers without MOVE
write directly into the final directory and rely on `_COMPLETE.json` as the
authoritative commit boundary.

The executor validates each canonical local file against its planned size and
SHA-256 before a fresh PUT, persists object state/evidence after every verified
remote object, and reconciles already-stored objects on restart. A lost PUT
response enters bounded remote-size polling and is accepted only if subsequent
remote metadata proves the expected size. This accommodates DAV frontends that
finish committing an upstream object after the HTTP response timeout without
causing a duplicate large-file PUT. `manifest.json` and `_COMPLETE.json` are deterministic canonical JSON;
when GET is advertised their exact remote content is checked as well as size.
A crash after MOVE but before the commit marker resumes against the final tree
without replaying already stored media.

`.staging/<package_id>` is therefore a transport detail, never a second archive
identity.
Only canonical MediaItem files enter `media/`; Telegram transport artifacts
(cover frames, thumbnails, remux temporaries, split segments and Telegram
split manifests) are intentionally outside the archive plan. `manifest.json`
contains archive-safe media facts and SHA-256 values but never source URLs,
captions, peer/user ids, credentials, destination configuration or local
absolute paths. `_COMPLETE.json` is written only after all objects and the
manifest have been verified, so a visible directory without that marker is
never considered a complete archive.

`ArchiveService` and `ArchiveRuntime` are the only live WebDAV application
path. `JobRunner` idempotently creates the package after publish planning; a
separate wakeable worker processes `planned/staging/uploading/verifying`
packages. Archive failures never mutate Telegram Job state. Failed packages are
not automatically looped forever: the owner explicitly uses
`/archive retry <job>` after local canonical cache is validated, which moves the
same package back to staging.

The old `BackupAttempt` domain/service/adapter and `/backup` UI have been
removed. The current schema no longer creates `backup_attempts` or
`backup_files`. Existing production databases are not destructively migrated,
so historical tables may remain on disk but no runtime code reads them.

Archive configuration uses only `TGVIO_ARCHIVE_*` variables and credentials
never enter the durable package, archive events, manifest, diagnostics, or
user-facing status. `TGVIO_ARCHIVE_ENABLED=false` is the source-safe default.
Production explicitly enables Archive V2 after the migrated endpoint passed
capability probing and two real fixtures, including a live background-runtime
commit that reached `committed` with verified media, manifest, and
`_COMPLETE.json` before cleanup.

Production cut over from the retired project on 2026-09-03. The old runtime is
kept only as a retirement archive, not as the authoritative implementation.
