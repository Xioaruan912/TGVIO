from __future__ import annotations

from dataclasses import replace
import logging
import time
import uuid
from typing import Any

from aiohttp import web

from tgvio_player.application.player_recovery import PlayerRecoveryService, RecoveryError, WebDavBootstrap
from tgvio_player.application.ports import WebDavWriteClient, WebDavWriteError
from tgvio_player.domain.storage_settings import PlayerStorageSettings, safe_storage_relpath, validate_webdav_endpoint
from .client import resolve_client
from .diagnostics import client_fingerprint

_LOG = logging.getLogger("tgvio_player.storage")


class PlayerStorageSettingsHttpMixin:
    async def _storage_retry(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        self._require_same_origin(request)
        self._enforce_storage_rate(request)
        if self._favorite_backup is None:
            raise web.HTTPServiceUnavailable(text="favorite sync unavailable")
        retried = await self._favorite_backup.retry_failed()
        return web.json_response({"retried": retried})

    async def _storage_settings_get(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        return web.json_response(await self._storage_settings_dto())

    async def _storage_settings_put(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        self._require_same_origin(request)
        self._enforce_storage_rate(request)
        if self._recovery_service is None or self._storage_client_factory is None:
            raise web.HTTPServiceUnavailable(text="storage settings unavailable")
        payload = await self._json_object(request)
        old = await self._repository.get_storage_settings()
        endpoint = payload.get("endpoint_url", old.endpoint_url)
        player_root = payload.get("player_root", old.player_root)
        favorites_dir = payload.get("favorites_dir", old.favorites_dir)
        archive_root = payload.get("archive_root", old.archive_root)
        old_libraries = await self._repository.get_libraries()
        libraries_input = payload.get("libraries")
        if libraries_input is not None:
            if (not isinstance(libraries_input, list) or len(libraries_input) > 20
                    or not all(isinstance(item, str) and len(item) <= 2048 for item in libraries_input)):
                raise web.HTTPBadRequest(text="invalid storage settings")
            archive_root = next((item for item in libraries_input if item.strip().strip("/")), "")
        username_input = payload.get("username", "")
        password_input = payload.get("password", "")
        if not all(isinstance(item, str) for item in (
            endpoint, player_root, favorites_dir, archive_root, username_input, password_input,
        )):
            raise web.HTTPBadRequest(text="invalid storage settings")
        if any(len(item) > 2048 for item in (endpoint, player_root, favorites_dir, archive_root)):
            raise web.HTTPBadRequest(text="invalid storage settings")
        if len(username_input) > 256 or len(password_input) > 1024:
            raise web.HTTPBadRequest(text="invalid storage credentials")
        try:
            endpoint = validate_webdav_endpoint(endpoint)
            # Folders are relative to the WebDAV root; stray slashes are not an error.
            player_root = safe_storage_relpath(player_root.strip().strip("/"))
            favorites_dir = safe_storage_relpath(favorites_dir.strip().strip("/"))
            archive_root = archive_root.strip().strip("/")
            if archive_root:
                safe_storage_relpath(archive_root)
            # The first library is the video folder (archive_root); an old client sending only
            # archive_root keeps the other libraries.
            raw = libraries_input if libraries_input is not None else [archive_root, *old_libraries[1:]]
            libraries = tuple(dict.fromkeys(
                safe_storage_relpath(item.strip().strip("/")) for item in raw if item.strip().strip("/")
            ))
            if "\x00" in username_input + password_input:
                raise ValueError
        except ValueError:
            raise web.HTTPBadRequest(text="invalid storage settings") from None

        current_username, current_password = self._recovery_service.credentials_for(old)
        username = username_input or current_username
        password = password_input or current_password
        credentials_changed = bool(username_input or password_input)
        if credentials_changed:
            if not username or not password:
                raise web.HTTPBadRequest(text="both WebDAV credentials are required")
            try:
                username_ciphertext, password_ciphertext = self._recovery_service.encrypt_credentials(
                    username, password,
                )
            except RecoveryError:
                raise web.HTTPBadRequest(text="invalid storage credentials") from None
        else:
            username_ciphertext = old.username_ciphertext
            password_ciphertext = old.password_ciphertext

        # Without explicit backup folders the backup sits in the video folder (place_backup_under).
        auto_backup = bool(archive_root) and "player_root" not in payload and "favorites_dir" not in payload
        # Only a new backup folder moves the backed-up favorites; a new address just saves.
        target_changed = not auto_backup and (player_root, favorites_dir) != (old.player_root, old.favorites_dir)
        archive_changed = archive_root != old.archive_root or libraries != old_libraries
        if (not target_changed and not credentials_changed and not archive_changed
                and endpoint == old.endpoint_url):
            return web.json_response(await self._storage_settings_dto())
        new_settings = PlayerStorageSettings(
            endpoint, player_root, favorites_dir,
            username_ciphertext, password_ciphertext, old.revision + 1, archive_root,
        )
        if archive_root and self._archive_check is not None:
            # A wrong folder must fail here, not empty the wall at the next catalog scan.
            try:
                for root in libraries:
                    await self._archive_check(replace(new_settings, archive_root=root))
            except Exception as exc:
                _LOG.warning(
                    "Player archive folder check failed: category=%s", type(exc).__name__,
                )
                raise web.HTTPBadRequest(text="archive folder could not be read") from None
        try:
            if target_changed:
                await self._recovery_service.migrate_target(new_settings)
                await self._repository.save_libraries(libraries)
            else:
                # With a video folder, reading it already proved the account; no test file is written.
                probe = (
                    await self._probe_storage_target(endpoint, player_root, username, password)
                    if credentials_changed and not (archive_root and self._archive_check) else {"ok": True}
                )
                if not probe["ok"]:
                    _LOG.warning(
                        "Player storage save preflight failed: operation=probe category=%s status=%s",
                        probe["category"], probe["status_code"],
                    )
                    raise RecoveryError("WebDAV credentials could not write the configured target")
                if auto_backup:
                    new_settings = await self._recovery_service.place_backup_under(new_settings)
                await self._repository.save_storage_settings(new_settings)
                await self._repository.save_libraries(libraries)
                try:
                    await self._recovery_service.export_state()
                except Exception as exc:
                    # The backup snapshot catches up on the next favorite sync; it never blocks a save.
                    _LOG.warning("Player state export after save failed: category=%s", type(exc).__name__)
            await self._replace_favorite_writer(new_settings)
            await self._apply_storage_settings()
        except Exception as exc:
            root_cause: BaseException = exc
            while root_cause.__cause__ is not None:
                root_cause = root_cause.__cause__
            if isinstance(root_cause, WebDavWriteError):
                operation = root_cause.operation
                category = root_cause.category
                status_code = root_cause.status_code
            else:
                operation = "storage_save"
                category = type(root_cause).__name__
                status_code = None
            _LOG.warning(
                "Player storage settings save failed: operation=%s category=%s status=%s",
                operation, category, status_code,
            )
            raise web.HTTPBadRequest(text="storage settings could not be saved") from None
        return web.json_response(await self._storage_settings_dto())

    async def _storage_settings_test(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        self._require_same_origin(request)
        self._enforce_storage_rate(request)
        if self._storage_client_factory is None or self._recovery_service is None:
            raise web.HTTPServiceUnavailable(text="storage test unavailable")
        payload = await self._json_object(request)
        old = await self._repository.get_storage_settings()
        endpoint = payload.get("endpoint_url", old.endpoint_url)
        root = payload.get("player_root", old.player_root)
        username_input = payload.get("username", "")
        password_input = payload.get("password", "")
        if not all(isinstance(item, str) for item in (endpoint, root, username_input, password_input)):
            raise web.HTTPBadRequest(text="invalid storage test settings")
        try:
            endpoint = validate_webdav_endpoint(endpoint)
            root = safe_storage_relpath(root.strip().strip("/"))
        except ValueError:
            raise web.HTTPBadRequest(text="invalid storage test settings") from None
        current_username, current_password = self._recovery_service.credentials_for(old)
        username = username_input or current_username
        password = password_input or current_password
        result = await self._probe_storage_target(endpoint, root, username, password)
        return web.json_response(result)

    async def _storage_recover(self, request: web.Request) -> web.Response:
        await self._authenticate(request)
        self._require_same_origin(request)
        self._enforce_storage_rate(request)
        if self._recovery_service is None:
            raise web.HTTPServiceUnavailable(text="recovery unavailable")
        payload = await self._json_object(request)
        endpoint = payload.get("endpoint_url")
        player_root = payload.get("player_root")
        username = payload.get("username")
        password = payload.get("password")
        if not all(isinstance(value, str) for value in (endpoint, player_root, username, password)):
            raise web.HTTPBadRequest(text="invalid recovery settings")
        try:
            result = await self._recovery_service.restore(
                WebDavBootstrap(endpoint, player_root.strip().strip("/"), username, password)
            )
        except RecoveryError:
            raise web.HTTPBadRequest(text="Player state recovery failed") from None
        await self._apply_storage_settings()
        return web.json_response({
            "restored": result.restored,
            "revision": result.revision,
            "favorite_count": result.favorite_count,
        })

    async def _storage_settings_dto(self) -> dict[str, object]:
        settings = await self._repository.get_storage_settings()
        summary = await self._repository.favorite_sync_summary()
        if int(summary["failed"] or 0):
            status = "failed"
        elif int(summary["pending"] or 0):
            status = "pending"
        else:
            status = "synced"
        return {
            "endpoint_url": settings.endpoint_url,
            "player_root": settings.player_root,
            "favorites_dir": settings.favorites_dir,
            "archive_root": settings.archive_root,
            "libraries": list(await self._repository.get_libraries()),
            "archive_configured": bool(settings.archive_root),
            "storage_configured": settings.endpoint_url != "https://webdav.example.invalid/dav",
            "credentials_configured": bool(
                settings.username_ciphertext and settings.password_ciphertext
            ),
            "revision": settings.revision,
            "sync_status": status,
            "pending_count": int(summary["pending"] or 0),
            "failed_count": int(summary["failed"] or 0),
            "last_success_at": summary["last_success_at"],
        }

    async def _probe_storage_target(
        self, endpoint: str, root: str, username: str, password: str
    ) -> dict[str, object]:
        assert self._storage_client_factory is not None
        writer = self._storage_client_factory(endpoint, username, password)
        probe_path = f"{safe_storage_relpath(root)}/.player-probe-{uuid.uuid4().hex}.bin"
        payload = b"TGVIO Player WebDAV permission probe"
        replacement = b"TGVIO Player WebDAV overwrite probe"
        category = "ok"
        operation = "mkdir"
        status_code: int | None = None
        ok = False
        try:
            await writer.ensure_directory(root)

            operation = "put"
            async def body(content: bytes):
                yield content

            first_receipt = await writer.put_stream(
                probe_path, body(payload), size_bytes=len(payload), content_type="application/octet-stream",
            )
            replacement_receipt = await writer.put_stream(
                probe_path, body(replacement), size_bytes=len(replacement),
                content_type="application/octet-stream",
            )
            operation = "stat"
            stat = await writer.stat(probe_path)
            operation = "get"
            fetched = await writer.get_bytes(probe_path, max_bytes=len(replacement))
            if (
                first_receipt.size_bytes != len(payload)
                or replacement_receipt.size_bytes != len(replacement) or stat is None
                or stat.size_bytes != len(replacement) or fetched != replacement
            ):
                category = "verification_mismatch"
            else:
                ok = True
        except WebDavWriteError as exc:
            operation = exc.operation
            category = exc.category
            status_code = exc.status_code
        except Exception:
            operation = "connection"
            category = "connection_error"
        finally:
            cleanup_operation = "delete"
            try:
                await writer.delete(probe_path)
            except WebDavWriteError as exc:
                if ok:
                    ok = False
                    operation = cleanup_operation
                    category = "cleanup_failed"
                    status_code = exc.status_code
            except Exception:
                if ok:
                    ok = False
                    operation = cleanup_operation
                    category = "cleanup_failed"
            close = getattr(writer, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass
        if not ok:
            _LOG.warning(
                "Player storage target probe failed: operation=%s category=%s status=%s",
                operation, category, status_code,
            )
        return {
            "ok": ok,
            "operation": None if ok else operation,
            "category": category,
            "status_code": status_code,
        }

    async def _apply_storage_settings(self) -> None:
        if self._on_storage_changed is not None:
            await self._on_storage_changed(await self._repository.get_storage_settings())

    async def _replace_favorite_writer(self, settings: PlayerStorageSettings) -> None:
        if self._favorite_backup is None or self._storage_client_factory is None:
            return
        assert self._recovery_service is not None
        username, password = self._recovery_service.credentials_for(settings)
        writer = self._storage_client_factory(settings.endpoint_url, username, password)
        previous = self._favorite_backup.replace_writer(writer)
        close = getattr(previous, "close", None)
        if close is not None:
            await close()

    def _enforce_storage_rate(self, request: web.Request) -> None:
        key = client_fingerprint(resolve_client(request))
        now = time.monotonic()
        attempts = [stamp for stamp in self._storage_rate.get(key, []) if now - stamp < 60]
        if len(attempts) >= 10:
            raise web.HTTPTooManyRequests(text="storage operation rate limited")
        attempts.append(now)
        self._storage_rate[key] = attempts
