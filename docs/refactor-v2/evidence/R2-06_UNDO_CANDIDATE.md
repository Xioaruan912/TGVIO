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

The post-candidate review tightened the operational boundary further:

- delete children in the discussion group before deleting the channel root;
- bound each Telegram delete to 10 seconds, retry transient failures once, stop after two consecutive terminal failures, and cap one confirmation run at 60 seconds;
- record every failed transport attempt even when the bounded automatic retry later succeeds;
- reject expired tokens during inspection as well as during atomic consumption;
- keep normal Job pages usable if undo status storage is temporarily unavailable, with a Chinese actionable error instead of an unhandled callback;
- bind cache cleanup tokens to the exact sorted Job-ID target set and pass that same set to execution, so a newly completed Job cannot be swept by an older confirmation page;
- stop reporting already-cleaned Job rows as cache-cleanup candidates.

No caption, URL, local path, credential, or message content is stored in operation-token payloads or revocation audit rows.

## Candidate validation completed so far

- post-review mounted-source foundation: `304` tests / `28.982s`, all passed;
- final fresh-image foundation: `290` tests / `35.867s` / `foundation_gates=passed`;
- v0/v1/v2/v3/v4 forward migration coverage to latest v5 passed;
- explicit v4 -> v5 rehearsal test preserves Job business counts and v4 backup identity;
- owner isolation, token expiry/single consumption, sibling invalidation, effect-revision invalidation, duplicate-effect dedupe and partial undo resume tests passed;
- callback payload length tests include undo/undo-confirm;
- retry, cancel, Archive retry and exact cache-cleanup token callbacks have explicit consume/replay/stale-state coverage;
- delete timeout, transient auto-retry, per-attempt audit, discussion-first ordering and repeated-failure circuit breaking have explicit tests;
- source/secret guard passed;
- architecture gate passed with `67` Python source files;
- `compileall` and `git diff --check` passed.

The remote schema-release pipeline now creates a SQLite Backup API rollback point, copies that rollback database into an ephemeral rehearsal directory, runs the candidate migration there, verifies source/target versions, applied migration set and before/backup schema identity, then deletes the rehearsal database directory before cutover. The formal release will therefore execute the production-derived v4 -> v5 rehearsal and fail closed before cutover if it differs from the declared migration.

The candidate was subsequently released as `r2-06-e065ad9-20260913T014301Z`. The formal production-derived v4 -> v5 rehearsal, 304-test network-disabled image gate, independent postflight and rollback asset check passed; see [R2-06_UNDO_RELEASE.md](R2-06_UNDO_RELEASE.md). No real Telegram message was deleted during candidate or release validation.

A read-only production-shape check found 188 valid visible Telegram effect rows across 19 Jobs, no malformed peer/message IDs and no duplicate targets; the largest current Job has 36 unique delete targets. Only aggregate counts were recorded.
