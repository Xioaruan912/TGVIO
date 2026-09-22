# R2-19 Player Production Release

Release date: 2026-09-22

## Release identity

- Initial Player implementation: `00f20ddcd24680be44425b9393c4ea038a49844e`
- Production startup correction: `cd6dda7ad83db5937d62e7c8c20094a2a0f1225d`
- Production Player image: `tgvio-player:r2-19-cd6dda7`
- Player release source: `/root/tgvio-player/releases/r2-19-cd6dda7/source`

## Isolation

- The Player is deployed as the independent `tgvio-player` Compose project.
- It binds only `127.0.0.1:8790`; Nginx is the only public entry point.
- Player data is isolated at `/root/tgvio-player/data`; it does not mount Bot data, downloads, logs, or session directories.
- The Player environment is separate from `/root/TGVIO/.env` and contains no Bot or Telethon settings.
- The Player uses its HTTPS-only WebDAV adapter through `https://csdn.im/dav`.
- Nginx allows only `GET`, `HEAD`, `PROPFIND`, and `OPTIONS` on `/dav`; all write methods return `405`.

## HTTPS

- Nginx serves `csdn.im` with a Let's Encrypt certificate.
- The certificate renewal timer is installed by Certbot.
- `/dav` proxies only the permitted read methods to the local OpenList service on `127.0.0.1:5244`.
- All other paths proxy to the loopback-only Player listener.

## Verification

- Player-specific isolated tests: 40 passed, 1 skipped before production release.
- Runtime settings/transport regression tests in the Player image: 4 passed.
- Player container: `running/healthy`.
- Bot container: `running/healthy`; its container ID was unchanged across Player deployments.
- Unauthenticated feed request: `401`.
- Authenticated feed request: `200`.
- Catalog smoke: 886 active videos.
- Real authenticated media Range request: `206 Partial Content` with `Content-Range`.

## Correction

The initial release waited for a full WebDAV catalog synchronization before opening the HTTP listener. With the production Archive size this exceeded the health-check window. `cd6dda7` moves the initial catalog synchronization into the existing background poll task, so health and authenticated UI access become available before catalog completion.

## Residual follow-up

The Player is restricted to the read-only HTTPS WebDAV route, but its current credential originated from the existing Archive configuration. Create a dedicated OpenList read-only account and replace only `/root/tgvio-player/player.env` in a maintenance window.

## Owner PIN Update

- Player update commit: `710ad08`.
- The Player accepts either a 32+ character access secret or an exact 9-digit owner PIN. The deployment uses an owner-provided PIN; its value is not recorded here.
- Failed login attempts are limited to five attempts per client within ten minutes, followed by a fifteen-minute temporary lockout.
- The Player-only image `tgvio-player:r2-19-pin-710ad08` was built from an isolated release source and deployed without changing the Bot container.
- Verification after cutover: Player `running/healthy`, Bot still `running` with the same container ID, health `200`, unauthenticated feed `401`, PIN login `200`, authenticated feed `200`, and public root `200`.
- The web login prompt now explicitly accepts a `9-digit Player PIN or Player access secret`.
