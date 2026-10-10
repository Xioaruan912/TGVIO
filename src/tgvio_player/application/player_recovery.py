from __future__ import annotations

import base64
import binascii
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any

from tgvio_player.application.ports import (
    PlayerCatalogRepository,
    PlayerStateCipherPort,
    WebDavWriteClient,
)
from tgvio_player.domain.storage_settings import (
    PlayerStorageSettings,
    safe_storage_relpath,
    validate_webdav_endpoint,
)


CONFIG_CONTEXT = b"tgvio-player-config-v1"
FAVORITES_CONTEXT = b"tgvio-player-favorites-v1"
USERNAME_CONTEXT = b"webdav-username"
PASSWORD_CONTEXT = b"webdav-password"
# The favorites manifest is the one snapshot whose *contents* grow with a feature:
# v2 adds user collections. Older payloads stay valid input (they carry no
# collections), and the encryption contexts above must never change with it - those
# strings are part of the key derivation, so bumping one would make every existing
# backup undecryptable.
MANIFEST_SCHEMA_VERSION = 2
_SUPPORTED_MANIFEST_VERSIONS = (1, MANIFEST_SCHEMA_VERSION)
_MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
# Snapshot revisions kept on the storage: the one the pointer names and two before it.
_KEPT_REVISIONS = 3
_MEDIA_ID_RE = re.compile(r"^[0-9a-f]{64}$")


class RecoveryError(RuntimeError):
    """Safe, user-facing failure category with no credentials or remote URL details."""


@dataclass(frozen=True, slots=True)
class WebDavBootstrap:
    endpoint_url: str
    player_root: str
    username: str
    password: str


@dataclass(frozen=True, slots=True)
class RecoverySnapshotReceipt:
    revision: int
    favorite_count: int


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    restored: bool
    revision: int
    favorite_count: int


@dataclass(frozen=True, slots=True)
class TargetMigrationResult:
    migrated: bool
    revision: int
    copied_files: int


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _decode_ciphertext(value: str | None) -> bytes | None:
    if value is None:
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RecoveryError("stored recovery credentials are invalid") from exc


def _join(root: str, relative: str) -> str:
    safe_root = safe_storage_relpath(root)
    safe_relative = safe_storage_relpath(relative)
    return f"{safe_root}/{safe_relative}"


async def _put_bytes(client: WebDavWriteClient, path: str, payload: bytes, content_type: str) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield payload

    await client.put_stream(path, chunks(), size_bytes=len(payload), content_type=content_type)


class EncryptedManifestStore:
    """Immutable encrypted revisions with a directly written, verified pointer."""

    def __init__(
        self,
        client: WebDavWriteClient,
        cipher: PlayerStateCipherPort,
        root: str,
        *,
        context: bytes = FAVORITES_CONTEXT,
        name: str = "favorites-manifest",
        schema_version: int = 1,
    ) -> None:
        self._client = client
        self._cipher = cipher
        self._root = safe_storage_relpath(root)
        if name not in {"favorites-manifest", "player-config"}:
            raise ValueError("invalid Player snapshot name")
        self._name = name
        self._context = context
        self._schema_version = schema_version

    async def load(self) -> dict[str, Any] | None:
        pointer_path = _join(self._root, f"{self._name}.enc")
        pointer_bytes = await self._client.get_bytes(pointer_path, max_bytes=4096)
        if pointer_bytes is None:
            return None
        try:
            pointer = json.loads(pointer_bytes)
            revision = pointer["revision"]
            filename = pointer["file"]
            digest = pointer["sha256"]
            if (
                pointer.get("schema_version") != 1 or type(revision) is not int or revision < 1
                or filename != f"{self._name}.{revision}.enc"
                or not isinstance(digest, str) or len(digest) != 64
            ):
                raise ValueError
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise RecoveryError("remote snapshot pointer is invalid") from exc
        encrypted = await self._client.get_bytes(_join(self._root, filename), max_bytes=_MAX_SNAPSHOT_BYTES)
        if encrypted is None or hashlib.sha256(encrypted).hexdigest() != digest:
            raise RecoveryError("remote snapshot revision is missing or corrupt")
        try:
            decoded = self._cipher.decrypt(encrypted, context=self._context)
            value = json.loads(decoded)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoveryError("remote snapshot cannot be authenticated") from exc
        if not isinstance(value, dict) or value.get("revision") != revision:
            raise RecoveryError("remote snapshot revision does not match its pointer")
        return value

    async def save_atomic(self, value: dict[str, Any]) -> None:
        revision = value.get("revision")
        if value.get("schema_version") != self._schema_version or type(revision) is not int or revision < 1:
            raise RecoveryError("Player snapshot schema or revision is invalid")
        current = await self.load()
        if current is not None:
            old_revision = int(current["revision"])
            if old_revision > revision:
                raise RecoveryError("refusing to replace a newer remote snapshot")
            if old_revision == revision:
                if current == value:
                    return
                raise RecoveryError("snapshot revision is already occupied")

        encrypted = self._cipher.encrypt(_json_bytes(value), context=self._context)
        filename = f"{self._name}.{revision}.enc"
        revision_path = _join(self._root, filename)
        existing = await self._client.get_bytes(revision_path, max_bytes=_MAX_SNAPSHOT_BYTES)
        if existing is not None:
            try:
                existing_value = json.loads(self._cipher.decrypt(existing, context=self._context))
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                existing_value = None
            if existing_value == value:
                encrypted = existing
            else:
                await _put_bytes(self._client, revision_path, encrypted, "application/octet-stream")
        else:
            await _put_bytes(self._client, revision_path, encrypted, "application/octet-stream")
        written_revision = await self._client.get_bytes(
            revision_path, max_bytes=_MAX_SNAPSHOT_BYTES,
        )
        if written_revision != encrypted:
            raise RecoveryError("remote snapshot revision verification failed")
        pointer = _json_bytes({
            "schema_version": 1,
            "revision": revision,
            "file": filename,
            "sha256": hashlib.sha256(encrypted).hexdigest(),
        })
        pointer_path = _join(self._root, f"{self._name}.enc")
        await _put_bytes(self._client, pointer_path, pointer, "application/json")
        written_pointer = await self._client.get_bytes(pointer_path, max_bytes=4096)
        if written_pointer != pointer:
            raise RecoveryError("remote snapshot pointer verification failed")
        await self._prune(revision)

    async def _prune(self, revision: int) -> None:
        """Keep the pointed revision and the two before it; older ones are never read.

        Every favourite change writes a revision, so without this the folder grows
        by one file per change. A failed delete is left for the next write.
        """
        stale = revision - _KEPT_REVISIONS
        if stale < 1:
            return
        try:
            await self._client.delete(_join(self._root, f"{self._name}.{stale}.enc"))
        except Exception:  # noqa: BLE001 - pruning never fails a snapshot write
            pass


class PlayerRecoveryService:
    def __init__(
        self,
        repository: PlayerCatalogRepository,
        cipher: PlayerStateCipherPort,
        client_factory: Callable[[str, str, str], WebDavWriteClient],
    ) -> None:
        self._repository = repository
        self._cipher = cipher
        self._client_factory = client_factory

    def encrypt_credentials(self, username: str, password: str) -> tuple[bytes, bytes]:
        if not username or not password:
            raise RecoveryError("WebDAV username and password are required")
        return (
            self._cipher.encrypt(username.encode("utf-8"), context=USERNAME_CONTEXT),
            self._cipher.encrypt(password.encode("utf-8"), context=PASSWORD_CONTEXT),
        )

    def credentials_for(self, settings: PlayerStorageSettings) -> tuple[str, str]:
        return (
            self._decrypt_credential(settings.username_ciphertext, USERNAME_CONTEXT),
            self._decrypt_credential(settings.password_ciphertext, PASSWORD_CONTEXT),
        )

    async def export_state(self) -> RecoverySnapshotReceipt:
        current = await self._repository.get_storage_settings()
        client = self._client_for_settings(current)
        try:
            root = safe_storage_relpath(current.player_root)
            favorites_path = safe_storage_relpath(current.favorites_dir)
            await client.ensure_directory(root)
            await client.ensure_directory(_join(root, favorites_path))
            locations = await self._repository.list_favorite_locations()
            location_by_id = {item[0]: item[1:] for item in locations}
            favorites = await self._list_favorites()
            pending_jobs = await self._repository.list_pending_favorite_sync()
            collections = await self._list_collections()
            config_store = EncryptedManifestStore(
                client, self._cipher, root, context=CONFIG_CONTEXT, name="player-config",
            )
            manifest_store = EncryptedManifestStore(
                client, self._cipher, root, schema_version=MANIFEST_SCHEMA_VERSION,
            )
            old_config = await config_store.load()
            old_manifest = await manifest_store.load()
            revision = max(
                current.revision,
                int(old_config["revision"]) if old_config else 0,
                int(old_manifest["revision"]) if old_manifest else 0,
            ) + 1
            settings = replace(current, revision=revision)
            config = self._config_snapshot(settings)
            manifest = self._manifest_snapshot(
                settings, revision, favorites, location_by_id, pending_jobs, collections,
            )
            await config_store.save_atomic(config)
            await manifest_store.save_atomic(manifest)
            for job in pending_jobs:
                if job.operation == "delete":
                    await self._repository.mark_favorite_delete_intent(job.media_id)
            await self._repository.save_storage_settings(settings)
            return RecoverySnapshotReceipt(revision, len(favorites))
        except RecoveryError:
            raise
        except Exception as exc:
            raise RecoveryError("Player state export failed; the local revision was not advanced") from exc
        finally:
            await self._close(client)

    async def restore(self, bootstrap: WebDavBootstrap) -> RecoveryResult:
        try:
            endpoint = validate_webdav_endpoint(bootstrap.endpoint_url)
            root = safe_storage_relpath(bootstrap.player_root)
            if any(ord(char) < 32 for char in bootstrap.username + bootstrap.password):
                raise ValueError
        except ValueError as exc:
            raise RecoveryError("WebDAV recovery connection settings are invalid") from exc
        client = self._client_factory(endpoint, bootstrap.username, bootstrap.password)
        try:
            config_store = EncryptedManifestStore(
                client, self._cipher, root, context=CONFIG_CONTEXT, name="player-config",
            )
            manifest_store = EncryptedManifestStore(
                client, self._cipher, root, schema_version=MANIFEST_SCHEMA_VERSION,
            )
            config = await config_store.load()
            manifest = await manifest_store.load()
            if config is None or manifest is None:
                raise RecoveryError("remote Player state snapshot is incomplete")
            self._validate_snapshot(config, "Player config")
            self._validate_snapshot(manifest, "favorites manifest", versions=_SUPPORTED_MANIFEST_VERSIONS)
            revision = int(config["revision"])
            if int(manifest["revision"]) > revision:
                raise RecoveryError("remote Player snapshots have inconsistent revisions")
            local = await self._repository.get_storage_settings()
            if local.revision > revision:
                raise RecoveryError("remote Player state is older than local state")
            settings = self._settings_from_snapshot(config)
            if settings.player_root != root or settings.endpoint_url != endpoint:
                raise RecoveryError("bootstrap location does not match the remote Player config")
            self._decrypt_credential(settings.username_ciphertext, USERNAME_CONTEXT)
            self._decrypt_credential(settings.password_ciphertext, PASSWORD_CONTEXT)
            items = manifest.get("items")
            if not isinstance(items, list):
                raise RecoveryError("remote favorites manifest is invalid")
            # Validated before anything is written: a payload this build cannot read
            # must fail the restore while the old state is still intact.
            collections = self._read_collections(manifest)

            verified: list[tuple[str, str, int, str, int, str]] = []
            deleting: list[tuple[str, str, int, str, int]] = []
            pending: list[tuple[str, int]] = []
            for item in items:
                if not isinstance(item, dict):
                    raise RecoveryError("remote favorites manifest is invalid")
                media_id = item.get("media_id")
                relpath = item.get("relpath")
                state = item.get("state")
                created_at = item.get("created_at")
                if (
                    not isinstance(media_id, str) or not _MEDIA_ID_RE.fullmatch(media_id)
                    or type(created_at) is not int or created_at < 0
                ):
                    raise RecoveryError("remote favorite record is invalid")
                if state == "deleting":
                    if isinstance(relpath, str):
                        safe_relpath = safe_storage_relpath(relpath)
                        if not safe_relpath.startswith(settings.favorites_dir + "/"):
                            raise RecoveryError("remote deletion path is outside the configured favorites directory")
                        stat = await client.stat(_join(root, safe_relpath))
                        if stat is not None:
                            deleting.append((
                                media_id, safe_relpath, stat.size_bytes,
                                str(item.get("mime_type") or "video/mp4"), created_at,
                            ))
                    continue
                if state not in {"synced", "pending"}:
                    raise RecoveryError("remote favorite record has an unsupported state")
                if state == "pending":
                    pending.append((media_id, created_at))
                    continue
                if not isinstance(relpath, str):
                    raise RecoveryError("remote favorite path is invalid")
                safe_relpath = safe_storage_relpath(relpath)
                if not safe_relpath.startswith(settings.favorites_dir + "/"):
                    raise RecoveryError("remote favorite path is outside the configured favorites directory")
                mime_type = item.get("mime_type")
                if not isinstance(mime_type, str) or not mime_type.startswith("video/"):
                    raise RecoveryError("remote favorite media type is invalid")
                stat = await client.stat(_join(root, safe_relpath))
                expected_size = item.get("size_bytes")
                if stat is None or type(expected_size) is not int or stat.size_bytes != expected_size:
                    raise RecoveryError("remote favorite copy is missing or has the wrong size")
                verified.append((media_id, safe_relpath, stat.size_bytes, mime_type, created_at, state))

            await self._repository.save_storage_settings(replace(settings, revision=revision))
            for media_id, relpath, size, mime, created_at, _state in verified:
                await self._repository.restore_favorite_copy(media_id, relpath, size, mime, created_at)
            for media_id, created_at in pending:
                await self._repository.restore_pending_favorite(media_id, created_at)
            for media_id, relpath, size, mime, created_at in deleting:
                await self._repository.restore_favorite_copy(media_id, relpath, size, mime, created_at)
                await self._repository.set_global_favorite(media_id, False)
                await self._repository.enqueue_favorite_sync(media_id, "delete")
                await self._repository.mark_favorite_delete_intent(media_id)
            await self._restore_collections(collections)
            return RecoveryResult(True, revision, len(verified) + len(pending))
        except RecoveryError:
            raise
        except Exception as exc:
            raise RecoveryError("Player state restore failed; local state was not replaced") from exc
        finally:
            await self._close(client)

    async def migrate_target(self, new_settings: PlayerStorageSettings) -> TargetMigrationResult:
        old_settings = await self._repository.get_storage_settings()
        try:
            endpoint = validate_webdav_endpoint(new_settings.endpoint_url)
            new_root = safe_storage_relpath(new_settings.player_root)
            new_favorites = safe_storage_relpath(new_settings.favorites_dir)
            if new_settings.revision and new_settings.revision <= old_settings.revision:
                raise RecoveryError("new WebDAV settings revision is stale")
            new_settings = replace(
                new_settings,
                endpoint_url=endpoint,
                revision=max(new_settings.revision, old_settings.revision + 1),
            )
        except ValueError as exc:
            if isinstance(exc, RecoveryError):
                raise
            raise RecoveryError("new WebDAV destination is invalid") from exc
        old_client = self._client_for_settings(old_settings)
        new_client = self._client_for_settings(new_settings)
        try:
            await new_client.ensure_directory(new_root)
            await new_client.ensure_directory(_join(new_root, new_favorites))
            config_store = EncryptedManifestStore(
                new_client, self._cipher, new_root, context=CONFIG_CONTEXT, name="player-config",
            )
            manifest_store = EncryptedManifestStore(
                new_client, self._cipher, new_root, schema_version=MANIFEST_SCHEMA_VERSION,
            )
            existing_config = await config_store.load()
            existing_manifest = await manifest_store.load()
            new_settings = replace(
                new_settings,
                revision=max(
                    new_settings.revision,
                    old_settings.revision + 1,
                    int(existing_config["revision"]) + 1 if existing_config else 1,
                    int(existing_manifest["revision"]) + 1 if existing_manifest else 1,
                ),
            )
            locations = await self._repository.list_favorite_locations()
            migrated_locations: list[tuple[str, str, int, str]] = []
            for media_id, old_relpath, size_bytes, mime_type in locations:
                safe_old_relpath = safe_storage_relpath(old_relpath)
                if not safe_old_relpath.startswith(old_settings.favorites_dir + "/"):
                    raise RecoveryError("existing favorite path is outside its configured directory")
                filename = PurePosixPath(safe_old_relpath).name
                new_relpath = _join(new_favorites, filename)
                source_size, source_type, source_chunks = await old_client.open_stream(
                    _join(old_settings.player_root, old_relpath)
                )
                if source_size != size_bytes:
                    raise RecoveryError("existing favorite copy has changed size")
                digest = hashlib.sha256()

                async def checked_chunks():
                    async for chunk in source_chunks:
                        digest.update(chunk)
                        yield chunk

                await new_client.put_stream(
                    _join(new_root, new_relpath), checked_chunks(),
                    size_bytes=size_bytes, content_type=source_type or mime_type,
                )
                stat = await new_client.stat(_join(new_root, new_relpath))
                if stat is None or stat.size_bytes != size_bytes:
                    raise RecoveryError("favorite copy verification failed")
                destination_size, _destination_type, destination_chunks = await new_client.open_stream(
                    _join(new_root, new_relpath)
                )
                destination_digest = hashlib.sha256()
                read_bytes = 0
                async for chunk in destination_chunks:
                    destination_digest.update(chunk)
                    read_bytes += len(chunk)
                if destination_size != size_bytes or read_bytes != size_bytes or destination_digest.digest() != digest.digest():
                    raise RecoveryError("favorite copy checksum verification failed")
                migrated_locations.append((media_id, new_relpath, size_bytes, mime_type))

            favorite_rows = await self._list_favorites()
            pending_jobs = await self._repository.list_pending_favorite_sync()
            collections = await self._list_collections()
            config = self._config_snapshot(new_settings)
            location_by_id = {item[0]: item[1:] for item in migrated_locations}
            manifest = self._manifest_snapshot(
                new_settings, new_settings.revision, favorite_rows, location_by_id,
                pending_jobs, collections,
            )
            await config_store.save_atomic(config)
            await manifest_store.save_atomic(manifest)
            for job in pending_jobs:
                if job.operation == "delete":
                    await self._repository.mark_favorite_delete_intent(job.media_id)
            await self._repository.save_storage_settings(new_settings)
            for media_id, relpath, size, mime in migrated_locations:
                await self._repository.save_favorite_location(media_id, relpath, size, mime)
            return TargetMigrationResult(True, new_settings.revision, len(migrated_locations))
        except RecoveryError:
            raise
        except Exception as exc:
            raise RecoveryError("new WebDAV destination failed verification; old destination remains active") from exc
        finally:
            await self._close(old_client)
            if new_client is not old_client:
                await self._close(new_client)

    async def _list_favorites(self) -> list[tuple[str, int]]:
        rows: list[tuple[str, int]] = []
        before: tuple[int, str] | None = None
        while True:
            page = await self._repository.list_global_favorite_page(limit=500, before=before)
            if not page:
                break
            rows.extend(page)
            if len(page) < 500:
                break
            media_id, created_at = page[-1]
            before = created_at, media_id
        return rows

    async def _list_collections(self) -> list[dict[str, Any]]:
        """Every collection with its members, paged so one large collection cannot
        turn an export into an unbounded read."""
        payload: list[dict[str, Any]] = []
        for collection in await self._repository.list():
            members: list[str] = []
            offset = 0
            while True:
                page = await self._repository.items(collection.collection_id, 500, offset)
                members.extend(page)
                if len(page) < 500:
                    break
                offset += len(page)
            payload.append({
                "name": collection.name,
                "kind": collection.kind,
                "rules_json": collection.rules_json,
                "items": members,
            })
        return payload

    @staticmethod
    def _read_collections(manifest: dict[str, Any]) -> list[dict[str, Any]]:
        raw = manifest.get("collections")
        if raw is None:
            # A v1 manifest has no collections at all; that is not an error.
            return []
        if not isinstance(raw, list):
            raise RecoveryError("remote collections manifest is invalid")
        collections: list[dict[str, Any]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                raise RecoveryError("remote collections manifest is invalid")
            name, kind = entry.get("name"), entry.get("kind")
            rules, items = entry.get("rules_json"), entry.get("items")
            if (
                not isinstance(name, str)
                or kind not in {"manual", "smart"}
                or (rules is not None and not isinstance(rules, str))
                or not isinstance(items, list)
                or any(
                    not isinstance(item, str) or not _MEDIA_ID_RE.fullmatch(item)
                    for item in items
                )
            ):
                raise RecoveryError("remote collection record is invalid")
            collections.append(
                {"name": name, "kind": kind, "rules_json": rules, "items": items}
            )
        return collections

    async def _restore_collections(self, collections: list[dict[str, Any]]) -> None:
        """Collections are matched by name and kind, never overwritten.

        The snapshot's copy of a collection is a stale echo of an editable field, so
        an existing row keeps what the user has since made of it and only gains the
        members it is missing. A member the catalog has not supplied yet is restored
        as an inactive placeholder, exactly like a pending favourite.
        """
        if not collections:
            return
        known = {
            (collection.name, collection.kind): collection.collection_id
            for collection in await self._repository.list()
        }
        for entry in collections:
            key = (entry["name"], entry["kind"])
            collection_id = known.get(key)
            if collection_id is None:
                try:
                    created = await self._repository.create(
                        entry["name"], entry["kind"], entry["rules_json"]
                    )
                except ValueError as exc:
                    raise RecoveryError("remote collection cannot be restored") from exc
                collection_id = created.collection_id
                known[key] = collection_id
            for media_id in entry["items"]:
                await self._repository.restore_item(collection_id, media_id)

    def _config_snapshot(self, settings: PlayerStorageSettings) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "revision": settings.revision,
            "endpoint_url": settings.endpoint_url,
            "player_root": settings.player_root,
            "favorites_dir": settings.favorites_dir,
            "username_ciphertext": base64.b64encode(settings.username_ciphertext).decode()
            if settings.username_ciphertext is not None else None,
            "password_ciphertext": base64.b64encode(settings.password_ciphertext).decode()
            if settings.password_ciphertext is not None else None,
        }

    def _manifest_snapshot(
        self,
        settings: PlayerStorageSettings,
        revision: int,
        favorites: list[tuple[str, int]],
        location_by_id: dict[str, tuple[str, int, str]],
        pending_jobs: list[Any],
        collections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for media_id, created_at in favorites:
            location = location_by_id.get(media_id)
            if location is None:
                items.append({
                    "media_id": media_id, "created_at": created_at,
                    "state": "pending", "revision": revision,
                })
            else:
                relpath, size_bytes, mime_type = location
                safe_relpath = safe_storage_relpath(relpath)
                items.append({
                    "media_id": media_id, "relpath": safe_relpath,
                    "size_bytes": size_bytes, "mime_type": mime_type,
                    "created_at": created_at, "state": "synced", "revision": revision,
                })
        for job in pending_jobs:
            if job.operation != "delete":
                continue
            location = location_by_id.get(job.media_id)
            if location is None:
                continue
            relpath, size_bytes, mime_type = location
            items.append({
                "media_id": job.media_id,
                "relpath": safe_storage_relpath(relpath),
                "size_bytes": size_bytes,
                "mime_type": mime_type,
                "created_at": 0,
                "state": "deleting",
                "revision": revision,
            })
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "revision": revision,
            "items": items,
            "collections": collections,
        }

    def _settings_from_snapshot(self, config: dict[str, Any]) -> PlayerStorageSettings:
        try:
            endpoint = validate_webdav_endpoint(config["endpoint_url"])
            root = safe_storage_relpath(config["player_root"])
            favorites = safe_storage_relpath(config["favorites_dir"])
            username = _decode_ciphertext(config.get("username_ciphertext"))
            password = _decode_ciphertext(config.get("password_ciphertext"))
        except (KeyError, TypeError, ValueError) as exc:
            raise RecoveryError("remote WebDAV settings are invalid") from exc
        return PlayerStorageSettings(endpoint, root, favorites, username, password, int(config["revision"]))

    def _validate_snapshot(
        self, value: dict[str, Any], label: str, *, versions: tuple[int, ...] = (1,)
    ) -> None:
        if value.get("schema_version") not in versions:
            raise RecoveryError(f"{label} schema version is unsupported")
        if type(value.get("revision")) is not int or value["revision"] < 1:
            raise RecoveryError(f"{label} revision is invalid")

    def _client_for_settings(self, settings: PlayerStorageSettings) -> WebDavWriteClient:
        username = self._decrypt_credential(settings.username_ciphertext, USERNAME_CONTEXT)
        password = self._decrypt_credential(settings.password_ciphertext, PASSWORD_CONTEXT)
        return self._client_factory(settings.endpoint_url, username, password)

    def _decrypt_credential(self, encrypted: bytes | None, context: bytes) -> str:
        if encrypted is None:
            return ""
        try:
            return self._cipher.decrypt(encrypted, context=context).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise RecoveryError("stored recovery credentials cannot be decrypted") from exc

    async def _close(self, client: object) -> None:
        close = getattr(client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result
