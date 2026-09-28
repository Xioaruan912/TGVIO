# R2-19 Player Public Baseline — Player-only Production Deployment

Release date: 2026-09-28 (UTC)
Scope: Player companion service only. The Telegram Bot was not rebuilt, restarted or reconfigured.

## Release identity

| Item | Value |
|---|---|
| Release id | `public-baseline-20260928-ac0ec51` |
| Full Git commit | `ac0ec518fa9a706e50bdd9016a626fa6550be0f0` (clean `origin/main`) |
| Source archive | `git archive` of that commit, 702,561 bytes, SHA-256 `f19acec39056b2572a5c352683d235cd7d763fdd7723999e10a870ce38461ed5` |
| Image archive transferred | `docker save` + gzip, 66,538,042 bytes, SHA-256 `50dc59adea503c4bf8df37a8d8e9d8711d0170dc95e2ae8fa3653b30d589098d` |
| Image id built on the control host | `sha256:e3ce359a14d2ee953568d2b30f7b0d6b1eaf840ba928312ef07d7a331bea7cb2` |
| Image id after `docker load` on the host | `sha256:6c3bb2fc07b5b4ede403eb7fe401bdc18fcb79aaa12efae218e554039a740f1b` |
| Runtime revision label | `ac0ec518fa9a706e50bdd9016a626fa6550be0f0` |
| Runtime version label | `public-baseline-20260928-ac0ec51` |
| In-container Player content manifest | 51 files, combined SHA-256 `85a154244244eb0852d4d19dcfe479d298bde3d49152d29f7a0b2aecc4ce0673` |
| Deployed source tree | `/root/tgvio-player/releases/public-baseline-20260928-ac0ec51` (mode 700, 4.2 MB) |

### Image id difference between control host and production (resolved, not a content difference)

`docker save` on the control host uses the containerd/OCI path while the production daemon loads into the classic store, which re-serialises the image config JSON (for example the remote config omits the `Entrypoint` key that the local config carries as `null`). The config blob digest therefore changes, although nothing else does. Evidence:

- All **10 layer digests are identical** on both sides (first layer `sha256:411a8667…`, last `sha256:beb5a470…`).
- Runtime config is identical: `Cmd=["python","-m","tgvio_player.main"]`, `User=65532:65532`, `WorkingDir=/app`, same 9 environment entries, same three `org.opencontainers.image.*` labels.
- The in-container content manifest is byte-identical on the control-host image, the transferred image, and the **currently running container** (51 files, `85a15424…`).

The loaded image id `sha256:6c3bb2fc…` is the authoritative production identity recorded here.

## Read-only preflight (2026-09-28T13:53Z)

- Host clock `2026-09-28T13:53:59Z`, `NTPSynchronized=yes`; single filesystem with 30.0 GB free.
- Bot: container `9482d376adaa054e0758ea2dad031c12884f00f3faeac4408aedb77e6694c063`, `running healthy`, restarts `0`, started `2026-09-28T06:29:11Z`, image `bb80c41da18e`, release `r2-46-4d1de27-20260928T062735Z`.
- Player: container `running healthy`, image `tgvio-player:playback-state-20260927-7077984` (`sha256:e835a0d569b07d6244fcf8ddc651acab6b5c03a075dbf27e343288011b68025d`, revision `7077984e475fdbcf56a77ca157119d11c1af44a9`), one instance, port published on `127.0.0.1:8790` only, data bind `/root/tgvio-player/data`.
- Reverse proxy: host nginx on `:80`/`:443` terminates the public hostname and forwards to the loopback port. nginx was not read, changed or restarted.
- Player env file `/root/tgvio-player/player.env`, mode `600`, root-owned; 22 keys, all `TGVIO_PLAYER_*`.
- Production Player ledger: `player_schema_migrations` rows 1–8 reproduce `LEGACY_MIGRATIONS` (version, name and checksum) **exactly**; `PRAGMA quick_check=ok`; `user_version=0`; 26 schema objects.
- Running Player source (`7077984`) versus the public commit: 38 of 41 Python files byte-identical; the only differences are the three files this release exists to add — `infrastructure/legacy_migration_checksums.py` (new), `infrastructure/migration.py` (lineage detection + v9 baseline) and `adapters/http/storage_settings.py` (`storage_configured`). `docker-compose.player.yml` and `Dockerfile.player` hashes match the repository exactly.
- 78 prior Player release directories, 50 prior `player.env.bak-*` files and ~100 prior Player image tags were present; nothing was deleted.

## Build and transfer

- Built once on the control host from the pushed commit with `scripts/player_release.sh --tag tgvio-player:public-baseline-20260928-ac0ec51 --commit ac0ec518fa9a… --release-id public-baseline-20260928-ac0ec51`. No rebuild happened afterwards.
- Offline checks on the image (`--network none`): `linux/amd64`; `tgvio_player` importable from `/app/src`; `player-web/index.html` present with `assets/index-DbQT9Ixa.js` and `assets/index--uz5C5Xk.css`; no `/app/src/tgvio`, `/app/tests`, `/app/player`, `.env` or `session` paths; starting it with no environment fails closed with `TGVIO_PLAYER_ENABLED must be true`.
- Image and source archive were transferred over the pinned HostDZire SSH key, verified by SHA-256 on the host, then `docker load`ed / extracted. Temporary incoming files were removed on both sides.

## Backup and rehearsal

- Root-only online backup via the SQLite backup API: `/root/tgvio-player/backups/player-pre-public-baseline-20260928-ac0ec51.sqlite3`, 39,596,032 bytes, mode `600` (directory `700`), SHA-256 `30c04db4f63fc305c413f327ecaff43933025ed2682053aca15dbb948615639a`.
- Env rollback copy: `/root/tgvio-player/player.env.bak-public-baseline-20260928-ac0ec51`, mode `600`, 818 bytes. Previous source `/root/tgvio-player/releases/playback-state-20260927-7077984` retained.
- Backup baseline: `quick_check=ok`, 26 schema objects, schema SHA-256 `5fa4d47cb7d75edabbab727b0585515be0d73bb181b7ea61b77dbb7f8c431bd1`, ledger 1–8, 16 business tables.
- Rehearsal, new runner against a copy of the backup, no listeners started: ledger unchanged, schema unchanged, all 16 table row counts unchanged → the legacy lineage is recognised and **nothing is replayed**.
- Negative control (checksum of version 8 set to zeros): rejected with `player legacy migration ledger is incomplete or mismatched` → fail closed.
- Positive control (empty database): applies `0009_player_public_baseline` only, producing 26 schema objects and one `player_storage_settings` row seeded with the `.invalid` sentinel.

## Cutover

- Env update touched exactly one line (`TGVIO_PLAYER_IMAGE`) through a `0600` sibling file plus `os.replace`; line count 22 → 22, mode unchanged `600`, Bot variables absent.
- Previous running image tagged `tgvio-player:rollback-public-baseline-20260928-ac0ec51` → `sha256:e835a0d5…` (verified equal to the image that was running).
- `scripts/player_deploy.sh --env-file /root/tgvio-player/player.env --execute` → `player_deploy=started bot_container_unchanged=true`.
- New container `5198d85d08d8027505a58dc55659bbfc45e73f4ff192394cab8148b7cfe01f01`, started `2026-09-28T14:02:20Z`, `running healthy`, restarts `0`, one instance.

## Postflight

- Bot unchanged: id `9482d376adaa…` (same as preflight), `running healthy`, restarts `0`, started `2026-09-28T06:29:11Z` — long before the cutover.
- Logs since restart: 0 tracebacks, 0 `ERROR`, 0 `CRITICAL`, 0 "startup refused"; `TGVIO Player listening …` and `player.warm.started total=931` present.
- Database: `quick_check=ok`; ledger still 8 rows with identical checksums; schema identity identical to the pre-cutover backup. 13 of 16 tables have identical counts; the three that moved (`player_sessions` 1→3, `feed_sessions` 1→3, `feed_recent_media` 20→3) are the direct result of this report's own login/feed smoke and are the same writes a normal browser session makes. No migration was applied.
- HTTP over loopback: `/healthz` 200 (`no-store`), unauthenticated `/api/v1/feed` 401, `/` 200 (`no-cache`).
- Authenticated: login 200 with `HttpOnly; Secure; SameSite=Strict; Path=/`; `/api/v1/feed?limit=3` 200 with 3 items and `next_cursor`; media DTO exposes only opaque `id` plus a **relative** `stream_url` (no remote path, WebDAV endpoint, Telegram id or credential); `GET /api/v1/media/{id}/stream` with `Range: bytes=0-1023` returned **206** with `Content-Range: bytes 0-1023/45626007`, `Accept-Ranges: bytes`, `Content-Type: video/mp4` — real WebDAV-backed streaming, not a synthetic response.
- New code path on the legacy database: `GET /api/v1/settings/storage` 200 with `storage_configured=true` and `revision=14`, i.e. the existing production destination is still recognised as configured rather than being mistaken for the public `.invalid` sentinel.
- Frontend served: `assets/index-DbQT9Ixa.js`, `assets/index--uz5C5Xk.css` — the assets built into this release.
- Public entrypoint through nginx: `https://<public host>/healthz` 200, `/` 200, `/api/v1/feed` 401 when unauthenticated.
- Container environment contains only `TGVIO_PLAYER_*` plus Python/`PATH` variables — no `BOT_TOKEN`, Telegram API credentials or session paths.
- Free disk after deployment: 29.8 GB.

## Rollback

Assets retained, all root-only:

- Previous image tag `tgvio-player:rollback-public-baseline-20260928-ac0ec51` (`sha256:e835a0d5…`), plus the original tag `tgvio-player:playback-state-20260927-7077984`.
- Previous source `/root/tgvio-player/releases/playback-state-20260927-7077984`.
- Database backup and env backup listed above.

Rollback is Player-only and does not restore the database unless corruption is proven (this release applied no schema or data migration):

```sh
docker tag tgvio-player:rollback-public-baseline-20260928-ac0ec51 tgvio-player:rollback-target
# switch TGVIO_PLAYER_IMAGE back through the same 0600 sibling + os.replace path
scripts/player_rollback.sh --env-file /root/tgvio-player/player.env \
  --image tgvio-player:rollback-public-baseline-20260928-ac0ec51 --execute
```

## Deviations and residual risk

- **Step order.** The plan lists backup/rehearsal before build/transfer; this run transferred the exact release artifacts first and then rehearsed against them, so the rehearsal exercised the deployed source. No cutover happened before a passing rehearsal.
- **Build location.** `scripts/player_release.sh` ran on the control host (WSL) rather than on the VPS; the exact image was transferred. The plan's wording allows this, but the image id difference above must be understood before comparing ids across hosts.
- **Not re-verified on a real device.** The API, auth, Range and static-asset layers were verified end to end, but no mobile-browser playback session was run for this deploy. The browser bundle differs from the previous release only by the settings-page "尚未配置收藏存储" state, which cannot trigger on a configured production database.
- **New-install path.** The fresh-database behaviour (public baseline + `.invalid` destination) was proven in the rehearsal, not in production, because production keeps its legacy database by design.
- **Not committed.** `docs/` is ignored by `.gitignore` in this repository, so this evidence file is untracked unless explicitly force-added.
