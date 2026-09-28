# Player Public Baseline and Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the VPS-priority Player source without newly exposing production storage paths, preserve exact legacy migration validation, then deploy the verified commit to VPS as a Player-only release.

**Architecture:** The public migration lineage begins at version 9 with a sanitized full schema. Existing Player databases remain on immutable versions 1–8, validated by exact name/SHA-256 attestations but never replayed; future migrations start at 10 and serve both lineages. Player settings and mocks use safe placeholders for new installs, while existing database settings remain untouched.

**Tech Stack:** Python 3.11, SQLite, asyncio, aiohttp, TypeScript/Vite, Docker Compose, SSH.

**Spec:** `docs/superpowers/specs/2026-09-28-player-public-migration-baseline-design.md`

## Global Constraints

- The VPS `player.sqlite3` and immutable release source are authoritative; do not edit legacy SQL, its ledger, or production settings.
- Never read, print, stage, or commit `.env`, Bot/Player tokens, Telegram sessions, WebDAV passwords, cookies, or SSH keys. Only the dedicated Player container may be deployed; Bot identity must remain unchanged.
- Public Git must contain no newly imported production WebDAV endpoint or Player archive/favorites path. Old Git history is not rewritten.
- Domain does not depend on SQLite, HTTP, filesystem, Telegram, or UI. Player remains a separate process/database with streamed HTTP Range and no Bot mounts.
- Use test-first changes and `apply_patch` for source edits. No real Telegram/WebDAV/site calls in default tests; no migration existing in the deployed legacy 1–8 chain may be altered.
- Release only from clean, pushed `origin/main`; transfer the sole approved Player image to VPS in the same stage. Back up Player SQLite and preserve old image/source before cutover; do not modify Bot `.env` or containers.

## Review Focus

- A legacy database with a valid 1–8 ledger and real data must open without replaying SQL or changing counts: Task 1 test and Task 5 rehearsal.
- A truncated, reordered, unknown, or tampered legacy ledger must fail closed before any write: Task 1 test.
- A fresh database must have all current tables/indexes but no real storage target: Task 2 integration test and private-schema comparison.
- An unconfigured new install must visibly require storage setup while keeping favorites retryable: Task 3 API/UI tests.
- A failed cutover or interrupted transfer must not restart Bot or silently reuse an unverified Player image: Task 5 pre/postflight and rollback checks.

---

### Task 1: Attest the immutable legacy migration lineage

**Files:**
- Modify: `src/tgvio_player/infrastructure/migration.py`
- Create: `src/tgvio_player/infrastructure/legacy_migration_checksums.py`
- Create: `tests/test_player_migration.py`

**Interfaces:**
- `run_migrations(connection: sqlite3.Connection, directory: Path) -> None` stays the public entry point.
- `LEGACY_MIGRATIONS: tuple[tuple[int, str, str], ...]` holds exact version/name/SHA-256 records; no legacy SQL text.
- On a complete legacy 1–8 ledger, verify and return without modifying schema or ledger. An incomplete/unknown ledger raises `PlayerMigrationError`.

- [ ] Write tests that create a minimal SQLite ledger with the eight independently pinned version/name/hash rows. Assert `run_migrations` preserves rows and rejects one altered hash, one missing version, one unknown version, and an existing business table without a ledger.
- [ ] Run `PYTHONPATH=src .venv/bin/python -m unittest tests.test_player_migration -v`; verify expected failures against the current runner, not import/setup errors.
- [ ] Add exact hash attestations, verified against the read-only VPS ledger and local private snapshot. Refactor `run_migrations` to classify existing legacy lineage before loading public SQL; preserve fail-closed error and transactional behavior.
- [ ] Run the focused tests and existing checksum test in `tests/test_player_catalog.py`; verify green. Do not delete or edit legacy SQL yet.

Legacy SHA-256 values, verified read-only against VPS in version order: `4cffbe1fd254b97993246c38215764f4786db22746cc49d864a542a8917d0718`, `aa9c6238a54f4d7deaccef81c11f5221fd1bdf3e9d702dd3f9ce1d6c5e22d8de`, `7eb8e7626277ebf924a9f2e48d0812dde342183f245e2dc08f87ed2a49166c01`, `9d6a99971603ba7ae3fe93c5f8ef6777f7d92cffb7eeb82207af97010f4acf88`, `8eeb717da6a87dfba37f07886b26a792101a32f5a416a76940cc949058be2d8a`, `f8e3466308e5a1fb77305e34edd109c40031645115e16053a91d5477b8bf6b83`, `5aa336eccd7d2a361b372fdd442f39949424d2303c43af2060cfa8287c61dade`, `247543875ea0abff8038aa646826132aff04f836720dbcb43dc4fb40203eeefd`.

### Task 2: Introduce the safe public baseline

**Files:**
- Create: `src/tgvio_player/infrastructure/migrations/0009_player_public_baseline.sql`
- Modify: `src/tgvio_player/infrastructure/migration.py`
- Modify: `tests/test_player_migration.py`, `tests/test_player_catalog.py`, `tests/test_player_backend.py`
- Remove from public working tree: `src/tgvio_player/infrastructure/migrations/0001_*.sql` through `0008_*.sql` after using the private snapshot to derive the v8 schema; do not remove them from the VPS immutable release.

**Interfaces:**
- `load_migrations(directory: Path) -> tuple[PlayerMigration, ...]` loads a public v9 baseline plus strictly ordered v10+ deltas, not legacy SQL.
- A fresh empty DB executes v9 atomically and records its exact hash. A public-lineage DB verifies v9; a legacy-lineage DB skips v9. Both apply contiguous v10+ migrations.
- `player_storage_settings` has `.invalid` example endpoint and generic relative folders; no production value.

- [ ] Add focused tests: empty DB records only v9, corrupted v9 checksum fails, nonempty untracked DB fails, and both lineages can apply a synthetic v10 once while rejecting gaps or mutated v10. Update `test_migration_checksum_change_fails_closed` to mutate a copied v9 file.
- [ ] Run the focused migration/catalog/backend tests and verify RED at behavior assertions.
- [ ] Derive full table/index/trigger SQL from the private 1–8 snapshot in a temporary database; create v9 with the same schema and safe seed. Use `apply_patch` for the checked-in SQL. Remove legacy files from the public working tree only after the schema comparison succeeds.
- [ ] Run tests. Compare normalized `sqlite_master` objects from private old-chain and public v9 databases, excluding only migration ledger and sequence state; compare table columns, indexes, foreign keys, and key Player repository operations. Record counts/hash only, never seed values or paths.

### Task 3: Make the new-install storage state explicit

**Files:**
- Modify: `src/tgvio_player/adapters/http/storage_settings.py`
- Modify: `player/web/src/api.ts`, `player/web/src/settings-page.ts`
- Modify: `player/web/tests/settings-page.test.mjs`, `tests/test_player_http.py`
- Modify: `player/web/README.md` (new-install setup note)

**Interfaces:**
- Add `storage_configured: bool` to `GET /api/v1/settings/storage`, computed from whether the persisted endpoint is the public `.invalid` sentinel. Existing production endpoint remains configured even at revision 0.
- `StorageSettingsDto.storage_configured` drives a clear “尚未配置收藏存储” state in the existing settings page; it does not add a page or transmit credentials to the browser.

- [ ] First add API and UI tests for a fresh public-baseline DB and a legacy DB; assert the former shows setup guidance, the latter remains configured, and failed favorite sync remains retryable after saving valid settings.
- [ ] Run focused Python and `npm test`; verify RED for the new state only.
- [ ] Implement the DTO flag and page message; keep existing settings save/test/retry flows and Player-only security boundaries. Replace test/mock production literals with `.invalid`/`example.test` fixtures.
- [ ] Run focused Python/JS tests and `npm run build`; verify green.

### Task 4: Sanitize and verify the complete Player import

**Files:**
- Modify as required: `tests/test_player_backend.py`, `tests/test_player_http.py`, `tests/test_player_media_reader.py`, `player/web/src/api.ts`, `player/web/tests/settings-page.test.mjs`.
- Create: `tests/test_player_public_source.py`.
- Include the VPS-priority Player source and tests already present in the working tree, but exclude local `node_modules`, build output, runtime data, `.env`, sessions, logs, and old sensitive SQL.

**Interfaces:** No new runtime interface; this task makes the imported source safe and reproducible.

- [ ] Add a static privacy regression test over Player source/fixtures, with only SHA-256 fingerprints and lengths of the exact production endpoint/root/favorites strings (never their plaintext) in the test. Verify it fails on the current unsanitized import and pin a separate assertion that the public v9 seed uses `.invalid`.
- [ ] Replace all source/mock/test literals with neutral fixtures and rerun the guard. Check `git diff --check`, `release_guard verify-tree .`, `release_guard architecture .`, `python3 -m unittest discover -s tests`, `sh scripts/check_foundation.sh`, `npm test`, and `npm run build`.
- [ ] Review explicit staging allowlist and `git diff --cached --name-status`; ensure no private file is staged, then commit Player import/compatibility. Run `scripts/build_check.sh` as a diagnostic, fix any failures, and push clean full commit to `origin/main`.

### Task 5: Rehearse and deploy the pushed Player commit

**Files:**
- Use: `scripts/player_release.sh`, `scripts/player_deploy.sh`, `scripts/player_rollback.sh`, `docker-compose.player.yml`.
- Write release evidence under `docs/refactor-v2/evidence/` only after actual checks; never include secrets, settings values, or remote media paths.

**Interfaces:** The deployed Player image label `org.opencontainers.image.revision` equals the pushed full commit; Bot container ID and Bot release identity remain unchanged.

- [ ] Read-only preflight: verify `origin/main` equals clean HEAD; record current Player/Bot IDs, prior Player image ID, Player schema ledger shape/hash match, `quick_check`, disk space, and rollback image/source availability. Do not expose DB content.
- [ ] On VPS, make a root-only SQLite online backup of `player.sqlite3` and preserve prior Player image/source/env metadata. Rehearse the new runner against the backup copy without starting listeners; assert schema/row counts and ledger unchanged.
- [ ] Build once from pushed full commit using `scripts/player_release.sh --tag tgvio-player:<release-id> --commit <full-commit> --release-id <release-id>`, transfer that exact image and `git archive` source to the VPS release directory, verify image ID/labels/source manifest. Update only `TGVIO_PLAYER_IMAGE` in the root-only Player env via a temporary 0600 sibling file plus `os.replace`, keeping a 0600 rollback copy; never print environment contents.
- [ ] Run `scripts/player_deploy.sh --env-file <root-only-player-env> --execute`. Check Player health/restarts/log errors, GET healthz, unauthenticated 401, authenticated feed and true Range 206 through existing secure session, old ledger unchanged, SQLite `quick_check=ok`, and Bot ID unchanged. If the authentication smoke is unavailable, mark it unverified; do not claim it passed.
- [ ] If postflight fails, first inspect state read-only, then use `scripts/player_rollback.sh` with the verified prior image only. Do not restore the database unless corruption is proven and owner impact is assessed. Record release identity, evidence, residual unverified checks, and rollback asset location without secrets.

## Self-review

The spec's privacy, immutable-migration, fresh-install, validation, Player-only deployment, and rollback requirements map respectively to Tasks 1–4, 1–2, 2–3, 4, and 5. The plan intentionally does not add Player product features or touch Bot code, Bot migrations, or Git history rewriting.
