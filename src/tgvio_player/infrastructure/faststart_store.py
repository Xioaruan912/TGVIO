from __future__ import annotations

import json
from pathlib import Path
import struct

from tgvio_player.application.faststart import FaststartOverlay

_VERSION = 1
_MAX_HEADER_BYTES = 4096


class FaststartStore:
    """File-backed cache of virtual faststart overlays.

    The file is ``<len(header)><json header><moov bytes>``. Overlays are keyed by
    the content-addressed ``media_id``, so entries are immutable and safe to keep
    for the lifetime of the catalog.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def _path(self, media_id: str) -> Path:
        return self._root / f"{media_id}.bin"

    def load(self, media_id: str) -> FaststartOverlay | None:
        path = self._path(media_id)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if len(data) < 4:
            return None
        header_length = struct.unpack(">I", data[:4])[0]
        if header_length < 2 or header_length > _MAX_HEADER_BYTES or 4 + header_length > len(data):
            return None
        try:
            header = json.loads(data[4 : 4 + header_length].decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        head = data[4 + header_length :]
        if header.get("v") != _VERSION or not isinstance(header.get("prefix_len"), int):
            return None
        if not isinstance(header.get("size"), int) or header.get("head_len") != len(head):
            return None
        mime = header.get("mime")
        return FaststartOverlay(
            prefix_len=int(header["prefix_len"]),
            head=head,
            size=int(header["size"]),
            mime=str(mime) if isinstance(mime, str) else "video/mp4",
        )

    def save(self, media_id: str, overlay: FaststartOverlay) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        header = json.dumps(
            {
                "v": _VERSION,
                "prefix_len": overlay.prefix_len,
                "head_len": overlay.front_len,
                "size": overlay.size,
                "mime": overlay.mime,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        payload = struct.pack(">I", len(header)) + header + overlay.head
        target = self._path(media_id)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_bytes(payload)
        temporary.replace(target)

    def delete(self, media_id: str) -> None:
        try:
            self._path(media_id).unlink()
        except FileNotFoundError:
            pass
