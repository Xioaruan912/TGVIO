# Refactoring notes

## Principles

- Telegram intake, job state, transport, backup and presentation should have
  separate responsibilities.
- A Telegram publish must not wait for WebDAV backup to finish.
- Every accepted job must reach a terminal state (success, failure or
  cancellation), otherwise FIFO upload ordering can deadlock.
- Runtime state in `session/` and media in `downloads/` must survive image
  rebuilds.

## Current boundaries

- `src/models.py`: job, album and session domain objects.
- `src/progress.py`: progress/position helpers plus per-job/per-phase generation,
  UI throttling, account token-bucket and DB-write gates.
- `src/storage.py`: atomic best-effort JSON persistence.
- `src/downloader.py`: cancellable yt-dlp Python-API adapter with immutable
  progress events, cooperative thread stop and validated per-job outputs.
- `src/media.py`: Telegram media download and publish transport plus URL adapter
  integration; URL progress reuses the shared U1 progress pipeline, while F2-B
  checkpoints every confirmed visible Telegram publish side effect before the
  overall publish transaction is considered complete.
- `src/webdav.py`: WebDAV protocol, integrity verification and retry.
- `src/views/`: pure Telegram renderers fed by immutable/basic view state,
  including stable home/task cards plus U2 durable queue/detail/failure/
  confirmation pages.
- `src/handlers/`: command, callback and private-message parsing/dispatch.
- `src/services/job_queue.py`: handler-facing queue transition facade plus home,
  progress, durable SQL queue/detail and revision-bound U2 operation seams.
- `src/services/operations.py`: user-scoped, expiring single-use confirmation
  tokens for destructive single/batch operations.
- `src/services/backup_manager.py`: handler-facing WebDAV lifecycle facade.
- `src/services/proxy_manager.py`: proxy settings/switching facade.
- `src/services/interactions.py`: revisioned input interaction sessions.
- `src/repository/`: SQLite lifecycle, checksum-verified forward migrations,
  schema 4 runtime/recovery metadata, revision-CAS transitions, atomic
  download/publish claims, claim heartbeat/interruption, local-cache
  persistence, accepted aggregates, events, published refs, backup and
  interaction-session DAOs. Production schema 4 exists at
  `session/state.sqlite3` and drives worker acquisition/restart classification.
- `src/services/recovery.py`: fail-closed startup recovery planner for queued,
  downloading, ready, publishing and interrupted durable jobs.
- `src/services/shadow_state.py`: serialized best-effort mirror from the legacy
  queue/WebDAV paths into SQLite, now using the explicit job state machine for
  durable lifecycle transitions.
- `src/state_machine.py`: authoritative allowed job-state transition table,
  terminal rules and pause/resume validation.
- `src/bot.py`: transport/orchestration compatibility layer; repository claims
  choose durable download/publish work, publisher payloads come from durable
  `job_items.local_path`, and process-local queues/Futures are only live
  wake-up/transport/UI compatibility mechanisms. It also owns bounded graceful
  shutdown and claim interruption.
- `src/main.py`: repository migrate/recovery before workers, explicit SIGTERM
  disconnect, then pipeline shutdown before repository close.

## Next extraction targets

1. F2-C: route initial WebDAV upload, automatic replay and manual retry through
   the shared error classifier and an independent backup retry budget.
2. Persist backup attempt/file error code, retry count and next retry time while
   preserving current remote-size idempotency checks and timeout-success logic.
3. F2-D: centralize proxy switching in a serial NetworkCoordinator and finish
   download/publish/backup budget isolation plus partial-publish UI actions.
4. Keep F3 disk quota/enforcement separate until F2 is fully tested and deployed.

## UI direction

- Keep one stable status message per job/session.
- Make every callback idempotent: stale buttons should report the current state
  instead of silently doing nothing.
- Show a compact summary first, with refresh and detail actions instead of
  rendering every internal field in one message.
- Keep destructive actions (`cancel`, remote delete, undo) behind a clear
  confirmation step where practical.
