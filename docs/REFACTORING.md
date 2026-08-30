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
- `src/bot.py`: current orchestration and Telegram handlers; this remains the
  next extraction target.

## Next extraction targets

1. Move queue state transitions into a `JobQueue` service with explicit states.
2. Move WebDAV lifecycle and log management into a `BackupManager` service.
3. Move command/callback rendering into UI view functions.
4. Add integration tests around cancellation, retry, cache retention and
   callback idempotency before changing the live Telegram flow.

## UI direction

- Keep one stable status message per job/session.
- Make every callback idempotent: stale buttons should report the current state
  instead of silently doing nothing.
- Show a compact summary first, with refresh and detail actions instead of
  rendering every internal field in one message.
- Keep destructive actions (`cancel`, remote delete, undo) behind a clear
  confirmation step where practical.
