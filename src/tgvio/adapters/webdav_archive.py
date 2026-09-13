from __future__ import annotations

from tgvio.adapters.webdav_archive_support import *  # noqa: F401,F403
from tgvio.adapters.webdav_client import WebDavClientMixin


class WebDavArchiveTransport(WebDavClientMixin):
    """HTTP/WebDAV mechanics for Archive V2; no package policy lives here."""

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        capability_root: str = "",
        timeout: float = 300.0,
        response_timeout: float = 600.0,
        chunk_bytes: int = 1024 * 1024,
        verify_attempts: int = 36,
        verify_interval_seconds: float = 10.0,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("WebDAV base URL must be absolute HTTP(S)")
        if parsed.username or parsed.password:
            raise ValueError("WebDAV base URL must not contain embedded credentials")
        self._parsed = parsed
        self._base_path = self._normalize_base_path(parsed.path or "/")
        self._capability_root = capability_root.strip().strip("/")
        if self._capability_root:
            self._relative_parts(self._capability_root)
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        self._authorization = f"Basic {token}"
        self._timeout = max(1.0, float(timeout))
        self._response_timeout = max(1.0, float(response_timeout))
        self._chunk_bytes = max(64 * 1024, int(chunk_bytes))
        self._verify_attempts = max(1, int(verify_attempts))
        self._verify_interval_seconds = max(0.0, float(verify_interval_seconds))
        self._capabilities: ArchiveCapabilities | None = None

    async def probe(self) -> ArchiveCapabilities:
        if self._capabilities is None:
            self._capabilities = await asyncio.to_thread(self._probe_sync)
        return self._capabilities

    async def ensure_collection(self, remote_path: str) -> None:
        await asyncio.to_thread(self._ensure_collection_sync, remote_path)

    async def stat(self, remote_path: str) -> ArchiveRemoteStat:
        return await asyncio.to_thread(self._stat_sync, remote_path)

    async def put_file(
        self,
        local_path: Path,
        remote_path: str,
        expected_size: int,
    ) -> ArchiveStoreReceipt:
        return await asyncio.to_thread(
            self._put_file_sync,
            Path(local_path),
            remote_path,
            int(expected_size),
        )

    async def put_bytes(
        self,
        payload: bytes,
        remote_path: str,
        *,
        content_type: str,
    ) -> ArchiveStoreReceipt:
        return await asyncio.to_thread(
            self._put_bytes_sync,
            bytes(payload),
            remote_path,
            content_type,
        )

    async def get_bytes(self, remote_path: str, *, max_bytes: int) -> bytes | None:
        return await asyncio.to_thread(self._get_bytes_sync, remote_path, max_bytes)

    async def move_collection(self, source_path: str, destination_path: str) -> None:
        await asyncio.to_thread(self._move_collection_sync, source_path, destination_path)

    async def delete_file(
        self,
        remote_path: str,
        *,
        expected_size: int,
        expected_etag: str | None = None,
        expected_sha256: str | None = None,
    ) -> ArchiveDeleteReceipt:
        return await asyncio.to_thread(
            self._delete_file_sync,
            remote_path,
            int(expected_size),
            expected_etag,
            expected_sha256,
        )

    def _probe_sync(self) -> ArchiveCapabilities:
        probe_path = (
            self._absolute_path(self._capability_root)
            if self._capability_root
            else self._base_path
        )
        status, headers, _body = self._request_sync("OPTIONS", probe_path)
        if status >= 400:
            raise WebDavArchiveError("WebDAV OPTIONS capability probe failed", status=status)
        allow = self._method_set(headers)
        prop_status, _prop_headers, body = self._request_sync(
            "PROPFIND",
            probe_path,
            body=self._propfind_body(),
            headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
        )
        propfind = prop_status in {200, 207}
        properties = self._parse_properties(body) if propfind else {}
        quota_used = self._optional_int(properties.get("quota-used-bytes"))
        quota_available = self._optional_int(properties.get("quota-available-bytes"))
        capabilities = ArchiveCapabilities(
            supports_propfind=propfind,
            supports_mkcol="MKCOL" in allow,
            supports_put="PUT" in allow,
            supports_move="MOVE" in allow,
            supports_get="GET" in allow,
            supports_etag=bool(properties.get("getetag")),
            supports_quota=quota_used is not None or quota_available is not None,
            quota_used_bytes=quota_used,
            quota_available_bytes=quota_available,
        )
        if propfind and not (capabilities.supports_mkcol and capabilities.supports_put):
            capabilities = self._probe_write_capabilities_sync(capabilities)
        return capabilities

    def _probe_write_capabilities_sync(
        self,
        baseline: ArchiveCapabilities,
    ) -> ArchiveCapabilities:
        """Verify omitted DAV methods in an isolated namespace and clean it up.

        Some WebDAV frontends expose a valid DAV endpoint but omit PUT/MKCOL/GET
        from the root OPTIONS Allow header.  Treating Allow as authoritative
        makes those servers unusable.  We therefore fall back to one bounded
        capability fixture under .staging, verify it, and immediately DELETE it.
        The archive runtime caches the result for the life of the process.
        """
        token = uuid.uuid4().hex[:12]
        prefix = f"{self._capability_root}/" if self._capability_root else ""
        root = f"{prefix}.staging/.capability-{token}"
        source = f"{root}/probe.bin"
        moved = f"{prefix}.staging/.capability-{token}-moved"
        payload = b"tgvio-archive-capability\n"
        supports_mkcol = baseline.supports_mkcol
        supports_put = baseline.supports_put
        supports_get = baseline.supports_get
        supports_move = baseline.supports_move
        supports_etag = baseline.supports_etag
        cleanup_paths = [moved, root]
        try:
            try:
                self._ensure_collection_sync(root)
                supports_mkcol = True
            except Exception:
                return baseline

            status, _headers, _body = self._request_sync(
                "PUT",
                self._absolute_path(source),
                body=payload,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(payload)),
                },
            )
            supports_put = status in {200, 201, 204}
            if supports_put:
                stat = self._stat_sync(source)
                supports_put = bool(stat.exists and stat.size_bytes == len(payload))
                supports_etag = supports_etag or bool(stat.etag)
                try:
                    get_status, _get_headers, get_body = self._request_sync(
                        "GET",
                        self._absolute_path(source),
                    )
                    supports_get = get_status == 200 and get_body == payload
                except Exception:
                    supports_get = False

            if supports_move and supports_put:
                try:
                    self._move_collection_sync(root, moved)
                    supports_move = self._stat_sync(f"{moved}/probe.bin").exists
                except Exception:
                    supports_move = False
        finally:
            for remote_path in cleanup_paths:
                try:
                    self._request_sync("DELETE", self._absolute_path(remote_path))
                except Exception:
                    pass

        return ArchiveCapabilities(
            supports_propfind=baseline.supports_propfind,
            supports_mkcol=supports_mkcol,
            supports_put=supports_put,
            supports_move=supports_move,
            supports_get=supports_get,
            supports_etag=supports_etag,
            supports_quota=baseline.supports_quota,
            quota_used_bytes=baseline.quota_used_bytes,
            quota_available_bytes=baseline.quota_available_bytes,
        )

    def _ensure_collection_sync(self, remote_path: str) -> None:
        parts = self._relative_parts(remote_path)
        current = self._base_path.rstrip("/")
        for part in parts:
            current = f"{current}/{part}" if current else f"/{part}"
            status, _headers, _body = self._request_sync("MKCOL", current)
            if status not in {200, 201, 204, 405}:
                raise WebDavArchiveError("WebDAV collection creation failed", status=status)

    def _stat_sync(self, remote_path: str) -> ArchiveRemoteStat:
        status, _headers, body = self._request_sync(
            "PROPFIND",
            self._absolute_path(remote_path),
            body=self._propfind_body(),
            headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
        )
        if status == 404:
            return ArchiveRemoteStat(exists=False)
        if status not in {200, 207}:
            raise WebDavArchiveError("WebDAV metadata lookup failed", status=status)
        properties = self._parse_properties(body)
        return ArchiveRemoteStat(
            exists=True,
            size_bytes=self._optional_int(properties.get("getcontentlength")),
            etag=properties.get("getetag") or None,
            is_collection=properties.get("resourcetype") == "collection",
        )

    def _put_file_sync(
        self,
        local: Path,
        remote_path: str,
        expected_size: int,
    ) -> ArchiveStoreReceipt:
        if not local.is_file():
            raise FileNotFoundError(local)
        actual_size = local.stat().st_size
        if actual_size != expected_size:
            raise ValueError("local archive source size changed")
        self._ensure_parent_sync(remote_path)
        absolute = self._absolute_path(remote_path)
        status: int | None = None
        try:
            status = self._stream_put_sync(local, absolute, actual_size)
        except Exception as exc:
            verified = self._wait_for_expected_size_sync(remote_path, actual_size)
            if not (verified.exists and verified.size_bytes == actual_size):
                if isinstance(exc, WebDavArchiveError):
                    raise
                raise WebDavArchiveError("WebDAV archive PUT result was not verifiable") from exc
            return ArchiveStoreReceipt(
                remote_path=remote_path,
                size_bytes=actual_size,
                verification_method="size",
                etag=verified.etag,
                reused_remote=False,
            )
        if status not in {200, 201, 204}:
            verified = self._wait_for_expected_size_sync(remote_path, actual_size)
            if not (verified.exists and verified.size_bytes == actual_size):
                raise WebDavArchiveError("WebDAV archive PUT failed", status=status)
        verified = self._wait_for_expected_size_sync(remote_path, actual_size)
        if not (verified.exists and verified.size_bytes == actual_size):
            raise WebDavArchiveError("WebDAV archive PUT failed size verification", status=status)
        return ArchiveStoreReceipt(
            remote_path=remote_path,
            size_bytes=actual_size,
            verification_method="size",
            etag=verified.etag,
            reused_remote=False,
        )

    def _put_bytes_sync(
        self,
        payload: bytes,
        remote_path: str,
        content_type: str,
    ) -> ArchiveStoreReceipt:
        self._ensure_parent_sync(remote_path)
        existing = self._stat_sync(remote_path)
        if existing.exists and existing.size_bytes == len(payload):
            current = self._get_bytes_sync(remote_path, max(len(payload), 1))
            if current == payload:
                return ArchiveStoreReceipt(
                    remote_path=remote_path,
                    size_bytes=len(payload),
                    verification_method="content",
                    etag=existing.etag,
                    reused_remote=True,
                )
        status, _headers, _body = self._request_sync(
            "PUT",
            self._absolute_path(remote_path),
            body=payload,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(payload)),
            },
        )
        if status not in {200, 201, 204}:
            raise WebDavArchiveError("WebDAV metadata PUT failed", status=status)
        verified = self._stat_sync(remote_path)
        if not (verified.exists and verified.size_bytes == len(payload)):
            raise WebDavArchiveError("WebDAV metadata PUT failed size verification", status=status)
        current = self._get_bytes_sync(remote_path, max(len(payload), 1))
        if current is not None and current != payload:
            raise WebDavArchiveError("WebDAV metadata PUT failed content verification")
        return ArchiveStoreReceipt(
            remote_path=remote_path,
            size_bytes=len(payload),
            verification_method="content" if current == payload else "size",
            etag=verified.etag,
            reused_remote=False,
        )

    def _get_bytes_sync(self, remote_path: str, max_bytes: int) -> bytes | None:
        status, headers, body = self._request_sync("GET", self._absolute_path(remote_path))
        if status == 404:
            return None
        if status not in {200, 206}:
            return None
        limit = max(1, int(max_bytes))
        declared = self._optional_int(headers.get("content-length"))
        if declared is not None and declared > limit:
            raise WebDavArchiveError("WebDAV metadata object exceeds read limit")
        if len(body) > limit:
            raise WebDavArchiveError("WebDAV metadata object exceeds read limit")
        return body

    def _move_collection_sync(self, source_path: str, destination_path: str) -> None:
        self._ensure_parent_sync(destination_path)
        status, _headers, _body = self._request_sync(
            "MOVE",
            self._absolute_path(source_path),
            headers={
                "Destination": self._destination_url(destination_path),
                "Overwrite": "F",
            },
        )
        if status not in {201, 204}:
            raise WebDavArchiveError("WebDAV collection MOVE failed", status=status)

    def _delete_file_sync(
        self,
        remote_path: str,
        expected_size: int,
        expected_etag: str | None,
        expected_sha256: str | None,
    ) -> ArchiveDeleteReceipt:
        if not self._relative_parts(remote_path):
            raise WebDavArchiveSafetyError("refusing to delete the WebDAV root")
        if expected_size < 0:
            raise ValueError("expected archive deletion size must be >= 0")
        current = self._stat_sync(remote_path)
        if not current.exists:
            return ArchiveDeleteReceipt(
                remote_path=remote_path,
                verification_method="already_absent",
                already_missing=True,
            )
        if current.is_collection:
            raise WebDavArchiveSafetyError("refusing to delete a WebDAV collection")
        if current.size_bytes != expected_size:
            raise WebDavArchiveSafetyError("remote archive deletion target size changed")
        if expected_etag is not None and current.etag != expected_etag:
            raise WebDavArchiveSafetyError("remote archive deletion target ETag changed")
        if expected_sha256:
            payload = self._get_bytes_sync(
                remote_path,
                max_bytes=max(1, expected_size),
            )
            if payload is None:
                raise WebDavArchiveSafetyError(
                    "remote archive deletion target content could not be verified"
                )
            if hashlib.sha256(payload).hexdigest() != expected_sha256:
                raise WebDavArchiveSafetyError("remote archive deletion target content changed")

        headers = {"If-Match": current.etag} if current.etag else None
        try:
            status, _headers, _body = self._request_sync(
                "DELETE",
                self._absolute_path(remote_path),
                headers=headers,
            )
        except Exception as exc:
            if self._wait_for_absent_sync(remote_path):
                return ArchiveDeleteReceipt(
                    remote_path=remote_path,
                    verification_method="absent_after_lost_response",
                )
            if isinstance(exc, WebDavArchiveError):
                raise
            raise WebDavArchiveError("WebDAV file DELETE result was not verifiable") from exc

        if status not in {200, 202, 204, 404}:
            if self._wait_for_absent_sync(remote_path):
                return ArchiveDeleteReceipt(
                    remote_path=remote_path,
                    verification_method="absent_after_error_response",
                )
            raise WebDavArchiveError("WebDAV file DELETE failed", status=status)
        if not self._wait_for_absent_sync(remote_path):
            raise WebDavArchiveError(
                "WebDAV file DELETE could not be verified",
                status=status,
            )
        return ArchiveDeleteReceipt(
            remote_path=remote_path,
            verification_method="absent_after_delete",
            already_missing=status == 404,
        )
