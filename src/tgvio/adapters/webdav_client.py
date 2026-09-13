from __future__ import annotations

from tgvio.adapters.webdav_archive_support import *  # noqa: F401,F403


class WebDavClientMixin:
    def _wait_for_expected_size_sync(
        self,
        remote_path: str,
        expected_size: int,
    ) -> ArchiveRemoteStat:
        last = ArchiveRemoteStat(exists=False)
        for attempt in range(self._verify_attempts):
            try:
                last = self._stat_sync(remote_path)
            except WebDavArchiveError:
                last = ArchiveRemoteStat(exists=False)
            if last.exists and last.size_bytes == expected_size:
                return last
            if attempt + 1 < self._verify_attempts and self._verify_interval_seconds > 0:
                time.sleep(self._verify_interval_seconds)
        return last

    def _wait_for_absent_sync(self, remote_path: str) -> bool:
        observed = False
        last_error: Exception | None = None
        for attempt in range(self._verify_attempts):
            try:
                current = self._stat_sync(remote_path)
            except Exception as exc:
                last_error = exc
            else:
                observed = True
                if not current.exists:
                    return True
            if attempt + 1 < self._verify_attempts and self._verify_interval_seconds > 0:
                time.sleep(self._verify_interval_seconds)
        if not observed and last_error is not None:
            raise WebDavArchiveError("WebDAV file DELETE verification was unavailable") from last_error
        return False

    def _stream_put_sync(self, local: Path, absolute_path: str, size: int) -> int:
        conn = self._connect()
        try:
            conn.putrequest("PUT", self._quote_path(absolute_path), skip_accept_encoding=True)
            conn.putheader("Authorization", self._authorization)
            conn.putheader("Content-Length", str(size))
            conn.putheader("Content-Type", "application/octet-stream")
            conn.endheaders()
            with local.open("rb") as handle:
                while chunk := handle.read(self._chunk_bytes):
                    conn.send(chunk)
            if conn.sock is not None:
                conn.sock.settimeout(self._response_timeout)
            response = conn.getresponse()
            response.read()
            return int(response.status)
        finally:
            conn.close()

    def _request_sync(
        self,
        method: str,
        absolute_path: str,
        *,
        body: bytes | str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        payload = body.encode("utf-8") if isinstance(body, str) else body
        request_headers = {"Authorization": self._authorization, **(headers or {})}
        conn = self._connect()
        try:
            conn.request(
                method,
                self._quote_path(absolute_path),
                body=payload,
                headers=request_headers,
            )
            response = conn.getresponse()
            data = response.read()
            return (
                int(response.status),
                {key.lower(): value for key, value in response.getheaders()},
                data,
            )
        finally:
            conn.close()

    def _ensure_parent_sync(self, remote_path: str) -> None:
        parts = self._relative_parts(remote_path)
        if len(parts) > 1:
            self._ensure_collection_sync("/".join(parts[:-1]))

    def _destination_url(self, remote_path: str) -> str:
        absolute = self._quote_path(self._absolute_path(remote_path))
        host = self._parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        default_port = 443 if self._parsed.scheme == "https" else 80
        port = self._parsed.port or default_port
        netloc = host if port == default_port else f"{host}:{port}"
        return urllib.parse.urlunsplit((self._parsed.scheme, netloc, absolute, "", ""))

    def _absolute_path(self, remote_path: str) -> str:
        relative = "/".join(self._relative_parts(remote_path))
        base = self._base_path.rstrip("/")
        if not relative:
            return base or "/"
        return f"{base}/{relative}" if base else f"/{relative}"

    @staticmethod
    def _relative_parts(value: str) -> list[str]:
        decoded = urllib.parse.unquote(str(value or "").strip())
        if "\\" in decoded or "\x00" in decoded:
            raise ValueError("unsafe WebDAV archive path")
        parts = [part for part in decoded.strip("/").split("/") if part]
        if any(
            part in {".", ".."} or re.search(r"[\x00-\x1f\x7f]", part)
            for part in parts
        ):
            raise ValueError("unsafe WebDAV archive path")
        return parts

    @staticmethod
    def _normalize_base_path(value: str) -> str:
        parts = WebDavClientMixin._relative_parts(value)
        return "/" + "/".join(parts) if parts else "/"

    @staticmethod
    def _quote_path(value: str) -> str:
        return urllib.parse.quote(value, safe="/")

    @staticmethod
    def _method_set(headers: dict[str, str]) -> set[str]:
        values = ",".join(
            value for key, value in headers.items() if key in {"allow", "public"}
        )
        return {part.strip().upper() for part in values.split(",") if part.strip()}

    @staticmethod
    def _propfind_body() -> bytes:
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<D:propfind xmlns:D="DAV:">'
            "<D:prop>"
            "<D:getcontentlength/><D:getetag/><D:resourcetype/>"
            "<D:quota-used-bytes/><D:quota-available-bytes/>"
            "</D:prop>"
            "</D:propfind>"
        ).encode("utf-8")

    @staticmethod
    def _parse_properties(body: bytes) -> dict[str, str]:
        if not body:
            return {}
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return {}
        result: dict[str, str] = {}
        for element in root.iter():
            name = element.tag.rsplit("}", 1)[-1].lower()
            if name == "resourcetype":
                if any(
                    child.tag.rsplit("}", 1)[-1].lower() == "collection"
                    for child in element
                ):
                    result[name] = "collection"
                continue
            if name in {
                "getcontentlength",
                "getetag",
                "quota-used-bytes",
                "quota-available-bytes",
            }:
                text = (element.text or "").strip()
                if text:
                    result[name] = text
        return result

    @staticmethod
    def _optional_int(value: str | None) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _connect(self) -> http.client.HTTPConnection:
        if self._parsed.scheme == "https":
            return http.client.HTTPSConnection(
                self._parsed.hostname,
                self._parsed.port or 443,
                timeout=self._timeout,
                context=ssl.create_default_context(),
            )
        return http.client.HTTPConnection(
            self._parsed.hostname,
            self._parsed.port or 80,
            timeout=self._timeout,
        )
