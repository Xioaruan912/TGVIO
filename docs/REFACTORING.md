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
- `src/progress.py`: progress/position presentation helpers.
- `src/storage.py`: atomic best-effort JSON persistence.
- `src/media.py`: Telegram media download and publish transport.
- `src/webdav.py`: WebDAV protocol, integrity verification and retry.
- `src/views/`: pure Telegram renderers fed by immutable/basic view state.
- `src/handlers/`: command, callback and private-message parsing/dispatch.
- `src/services/job_queue.py`: handler-facing queue transition facade over the
  existing in-memory pipeline.
- `src/services/backup_manager.py`: handler-facing WebDAV lifecycle facade.
- `src/services/proxy_manager.py`: proxy settings/switching facade.
- `src/services/interactions.py`: revisioned input interaction sessions.
- `src/repository/`: SQLite lifecycle, checksum-verified forward migrations,
  schema 3 runtime entities/claim metadata, revision-CAS transitions, atomic
  download/publish claims, accepted aggregates, events, published refs, backup
  and interaction-session DAOs. Production schema 3 exists at
  `session/state.sqlite3`; legacy workers are still the execution source of truth.
- `src/services/shadow_state.py`: serialized best-effort mirror from the legacy
  queue/WebDAV paths into SQLite, now using the explicit job state machine for
  durable lifecycle transitions.
- `src/state_machine.py`: authoritative allowed job-state transition table,
  terminal rules and pause/resume validation.
- `src/bot.py`: legacy in-memory worker/orchestration implementation plus thin
  dependency assembly for the extracted handlers/services.

## Next extraction targets

1. Add startup recovery classification before workers start, initially only for
   states/sources that can be recovered safely without Telegram history access.
2. Move download/publish worker acquisition to repository claims while keeping
   process-local queues/conditions only as wake-up mechanisms.
3. Add heartbeat/interrupted repair and restart tests for queued/downloading/
   ready/publishing, with partial published refs stopping automatic replay.
4. Add graceful shutdown and remove the legacy Future-based truth path only
   after repository-backed workers have survived a separate deployment cycle.
5. Keep the characterization suite unchanged while replacing legacy worker
   internals incrementally.

## UI direction

- Keep one stable status message per job/session.
- Make every callback idempotent: stale buttons should report the current state
  instead of silently doing nothing.
- Show a compact summary first, with refresh and detail actions instead of
  rendering every internal field in one message.
- Keep destructive actions (`cancel`, remote delete, undo) behind a clear
  confirmation step where practical.
