# R2-06 durable operation token / undo candidate

> Candidate baseline: `754de5cb84ada7520e3327b64cb008d38024e287`
> Scope: R2-06 remaining operation-token and Telegram publish undo contract
> Production before candidate: `r2-06-424aba8-20260913T002401Z`, SQLite v4

## Goal

Finish the remaining R2-06 destructive-action contract without deleting or rewriting original publish facts.

- destructive callbacks use owner-scoped, revision-bound, payload-hashed, expiring, single-consume durable operation tokens;
- undo derives only from durable confirmed Telegram publish effects;
- deletion is peer/message scoped and checkpointed after each external call;
- partial deletion can be resumed without deleting already-successful messages again;
- stale confirmation pages fail closed when Job/plan/archive/effect state changes.

## Candidate implementation

### Migration `0005_operation_tokens_undo`

Adds:

- `operation_tokens` with owner/action/resource/revision/payload hash/TTL/consumption fields;
- `publish_effect_revocations` with pending/deleted/failed checkpoints;
- append-only `publish_effect_revocation_events` for delete success/failure audit.

Original `publish_effects` stay immutable.

Expected normalized v5 schema SQL SHA-256:

`c70f05023d89fb89127070a4cffb7f6232609b578eb9e47e4d20fc79afb879c2`

### Generic operation-token semantics

Production confirmation flows are bound to a state snapshot and cannot be replayed after consumption/expiry. Consuming one token also atomically invalidates sibling confirmation tokens for the same owner/action/resource, preventing two open confirmation pages from both executing the same destructive action.

Tokenized callbacks include:

- Job retry;
- Job cancel;
- failed Archive retry;
- managed terminal-cache cleanup;
- controlled real fixture publish;
- Telegram publish undo.

### Publish undo

Undo only targets durable effects of type:

- `telegram_channel_message`;
- `telegram_discussion_message`.

`publish_step_receipts_committed` markers are ignored. Duplicate effect rows referring to the same `(peer_id, message_id)` are deleted once while all matching effect rows are checkpointed.

Each delete uses Telegram peer + message ID and `revoke=True`. After every external delete attempt the repository immediately records success/failure. A later retry reconstructs the remaining target set and never re-deletes a message already checkpointed as deleted.

No caption, URL, local path, credential, or message content is stored in operation-token payloads or revocation audit rows.

## Candidate validation completed so far

- mounted-source full unittest discovery: `289` tests, all passed;
- final fresh-image foundation: `290` tests / `35.867s` / `foundation_gates=passed`;
- v0/v1/v2/v3/v4 forward migration coverage to latest v5 passed;
- explicit v4 -> v5 rehearsal test preserves Job business counts and v4 backup identity;
- owner isolation, token expiry/single consumption, sibling invalidation, effect-revision invalidation, duplicate-effect dedupe and partial undo resume tests passed;
- callback payload length tests include undo/undo-confirm;
- source/secret guard passed;
- architecture gate passed with `67` Python source files;
- `compileall` and `git diff --check` passed.

The remote schema-release pipeline now creates a SQLite Backup API rollback point, copies that rollback database into an ephemeral rehearsal directory, runs the candidate migration there, verifies source/target versions, applied migration set and before/backup schema identity, then deletes the rehearsal database directory before cutover. The formal release will therefore execute the production-derived v4 -> v5 rehearsal and fail closed before cutover if it differs from the declared migration.

Formal production release and postflight are still pending at this candidate stage. No real Telegram message is deleted during candidate validation.
