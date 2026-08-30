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
  schema 4 runtime/recovery metadata, revision-CAS transitions, atomic
  download/publish claims, claim heartbeat, local-cache persistence, accepted
  aggregates, events, published refs, backup and interaction-session DAOs.
  Production schema 4 exists at `session/state.sqlite3` and now drives worker
  acquisition/restart classification.
- `src/services/recovery.py`: fail-closed startup recovery planner for queued,
  downloading, ready, publishing and interrupted durable jobs.
- `src/services/shadow_state.py`: serialized best-effort mirror from the legacy
  queue/WebDAV paths into SQLite, now using the explicit job state machine for
  durable lifecycle transitions.
- `src/state_machine.py`: authoritative allowed job-state transition table,
  terminal rules and pause/resume validation.
- `src/bot.py`: transport/orchestration compatibility layer; repository claims
  choose durable download/publish work while process-local queues/Futures still
  coordinate live transport and UI state.

## Next extraction targets

1. Add explicit graceful shutdown: stop intake/new claims, settle active claims
   as interrupted when needed, and drain WebDAV work within a bounded stop
   timeout.
2. Add SIGTERM/repeated-shutdown tests and restart verification that consumes
   the interrupted states created by shutdown.
3. Remove remaining places that treat Future/input_q membership as durable job
   truth; retain them only for in-process wake-up/transport coordination.
4. Mark R3 complete only after production stop/start/recovery smoke succeeds,
   then move to U1 without mixing UI work into shutdown cleanup.

## UI direction

- Keep one stable status message per job/session.
- Make every callback idempotent: stale buttons should report the current state
  instead of silently doing nothing.
- Show a compact summary first, with refresh and detail actions instead of
  rendering every internal field in one message.
- Keep destructive actions (`cancel`, remote delete, undo) behind a clear
  confirmation step where practical.
