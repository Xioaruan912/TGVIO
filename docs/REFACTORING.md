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
- `src/repository/`: R2 SQLite lifecycle, checksum-verified forward migrations,
  integrity checks and basic job/event/backup/settings DAOs. Production schema
  1 exists at `session/state.sqlite3`, but is not yet the runtime source of truth.
- `src/bot.py`: legacy in-memory worker/orchestration implementation plus thin
  dependency assembly for the extracted handlers/services.

## Next extraction targets

1. Add migration 2 for job items/texts/published refs/interaction sessions and
   atomically persist accepted job aggregates.
2. Shadow-write queue/backup lifecycle through the existing R1 service facades
   while the legacy in-memory pipeline remains authoritative.
3. Only after shadow state is verified, move durable queue/backup truth behind
   repositories and remove duplicated legacy mutations incrementally.
4. Add the explicit job state machine, idempotent transitions and restart
   recovery only after persistence is stable.
5. Keep the R0/R1 characterization suite unchanged while replacing the legacy
   `_Pipeline` internals incrementally.

## UI direction

- Keep one stable status message per job/session.
- Make every callback idempotent: stale buttons should report the current state
  instead of silently doing nothing.
- Show a compact summary first, with refresh and detail actions instead of
  rendering every internal field in one message.
- Keep destructive actions (`cancel`, remote delete, undo) behind a clear
  confirmation step where practical.
