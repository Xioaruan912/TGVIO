# R2-19D Local Deployment Artifacts

> Status: local-only scaffolding. It is not a production authorization, a Player deployment, or evidence that R2-19D is delivered.

These files prepare a Player-only deployment path without changing the Bot's
existing Compose contract:

- `Dockerfile.player` builds only `src/tgvio_player`; it contains neither
  `src/tgvio`, Telethon dependencies, nor Bot runtime files.
- `docker-compose.player.yml` defines only `tgvio-player`, uses a separate
  Compose project, maps `127.0.0.1:<port>` only, and mounts only the Player
  data directory at `/var/lib/tgvio-player`.
- `deploy/player.env.example` is a Player-only template. A real copy must stay
  outside the repository with mode `0600`, use an independent access secret,
  and use a read-only WebDAV account.
- `scripts/player_release.sh` only builds a local candidate image.
- `scripts/player_deploy.sh` and `scripts/player_rollback.sh` require
  `--execute`, target only `tgvio-player`, and compare the Bot container ID
  before and after the action. They refuse `/root/TGVIO/.env` and reject Bot
  variables in the Player environment file.

This Compose file includes an independent `/healthz` healthcheck. The Player
image now starts only `tgvio_player.main`, which owns a separate Player SQLite
catalog, HTTPS-only read-only WebDAV client, authenticated API, bounded startup
Range cache, and static frontend. It still must not be deployed until the
Player-only preflight, HTTPS reverse proxy configuration, and real-device smoke
evidence are complete. That remains separate from Bot production delivery.

When the runtime exists, the host-facing command shape is:

```sh
scripts/player_release.sh --tag tgvio-player:candidate
scripts/player_deploy.sh --env-file /root/tgvio-player.env --execute
scripts/player_rollback.sh --env-file /root/tgvio-player.env --image tgvio-player:previous --execute
```

Do not merge `docker-compose.player.yml` with `docker-compose.yml`, do not use
the Bot release tooling for Player cutovers, and do not add `session/`,
`downloads/`, `data/state.sqlite3`, `.env`, `BOT_TOKEN`, or Telethon settings
to the Player service.
