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

1. U1: build the stable `/start` control console from immutable/basic view
   state, keeping network probes out of synchronous rendering.
2. U1: converge per-job presentation on one main status card and persist its
   Telegram chat/message identifiers.
3. U1: replace the global progress edit throttle with `(job_id, phase)` state,
   generation guards and per-job Telegram edit throttling.
4. Keep U2 pagination/detail/confirmation flows separate until the U1 console,
   job card and throttling deployment is stable.

## UI direction

- Keep one stable status message per job/session.
- Make every callback idempotent: stale buttons should report the current state
  instead of silently doing nothing.
- Show a compact summary first, with refresh and detail actions instead of
  rendering every internal field in one message.
- Keep destructive actions (`cancel`, remote delete, undo) behind a clear
  confirmation step where practical.
