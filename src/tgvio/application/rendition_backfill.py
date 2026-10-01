"""Produce optional rendition sidecars without mutating committed packages."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Protocol

from tgvio.domain.renditions import (
    ALGORITHM, INDEX, SCHEMA, RenditionTask, canonical, safe_path,
)


class RenditionPort(Protocol):
    async def read_json(self, path: str) -> Any: ...
    async def download(self, path: str, target: Path, size: int, digest: str) -> None: ...
    async def recover(self, task: RenditionTask, heights: tuple[int, ...], work: Path,
                      max_bytes: int) -> dict[int, dict[str, Any]]: ...
    async def encode(self, source: Path, target: Path, height: int) -> dict[str, Any]: ...
    async def upload(self, source: Path, path: str) -> None: ...
    async def write_json(self, path: str, value: Any) -> None: ...
    async def exists(self, path: str, size: int) -> bool: ...


def bound_index(task: RenditionTask, value: Any) -> dict[str, Any]:
    if value is None:
        return {"schema": SCHEMA, "package_id": task.package_id,
                "manifest_sha256": task.manifest_hash, "algorithm": ALGORITHM, "media": []}
    if (not isinstance(value, dict) or value.get("schema") != SCHEMA
            or value.get("package_id") != task.package_id
            or value.get("manifest_sha256") != task.manifest_hash
            or value.get("algorithm") != ALGORITHM
            or not isinstance(value.get("media"), list)
            or any(not isinstance(e, dict) for e in value["media"])):
        raise ValueError("rendition index conflict")
    return value


class RenditionBackfill:
    def __init__(self, port: RenditionPort) -> None:
        self.port = port
        self.last_recovered = 0

    async def run(self, task: RenditionTask, work: Path) -> int:
        self.last_recovered = 0
        root = safe_path(task.root)
        source_path = safe_path(task.media["path"])
        await self._check_package(task)
        index = bound_index(task, await self.port.read_json(f"{root}/{INDEX}"))
        missing = []
        parent = task.media["sha256"]
        # Existing entries are reused only after checking their actual remote object.
        for height in task.required:
            matched = [e for e in index["media"] if e.get("variant_of") == parent
                       and e.get("height") == height]
            valid = False
            for entry in matched:
                path = safe_path(entry["path"])
                if not path.startswith("renditions/"):
                    raise ValueError("rendition object outside prefix")
                if await self.port.exists(f"{root}/{path}", int(entry["size_bytes"])):
                    valid = True
                    break
            if not valid:
                missing.append(height)
        if not missing:
            return 0
        # Reserve the original transfer: recovery and fallback together stay
        # within the existing 4 GiB source transfer allowance.
        recovery_budget = max(0, min(128 * 1024**2, 4 * 1024**3 - int(task.media["size_bytes"])))
        if not await self.port.exists(f"{root}/{source_path}", int(task.media["size_bytes"])):
            raise ValueError("source removed before recovery")
        recovered = await self.port.recover(task, tuple(missing), work, recovery_budget)
        produced = 0
        for height in missing[:]:
            if height not in recovered:
                continue
            spec = dict(recovered[height])
            relpath = safe_path(spec.pop("path"))
            if (not relpath.startswith(f"renditions/{parent[:16]}-{height}p-")
                    or not relpath.endswith(f"-{spec['sha256'][:16]}.mp4")
                    or spec["height"] != height or spec["size_bytes"] <= 0):
                raise ValueError("incorrect recovered rendition")
            await self._check_package(task)
            if not await self.port.exists(f"{root}/{source_path}", int(task.media["size_bytes"])):
                raise ValueError("source removed during recovery")
            if not await self.port.exists(f"{root}/{relpath}", spec["size_bytes"]):
                raise ValueError("recovered rendition disappeared")
            await self._publish(task, index, spec, relpath)
            produced += 1
            self.last_recovered += 1
            missing.remove(height)
        if not missing:
            return produced
        source = work / "source.mp4"
        await self.port.download(f"{root}/{source_path}", source,
                                 int(task.media["size_bytes"]), parent)
        for height in missing:
            output = work / f"{height}.mp4"
            spec = await self.port.encode(source, output, height)
            if spec["height"] != height or spec["size_bytes"] <= 0:
                raise ValueError("incorrect encoded rendition")
            relpath = f"renditions/{parent[:16]}-{height}p-{spec['sha256'][:16]}.mp4"
            if not await self.port.exists(f"{root}/{source_path}", int(task.media["size_bytes"])):
                raise ValueError("source removed during encode")
            await self.port.upload(output, f"{root}/{relpath}")
            if not await self.port.exists(f"{root}/{relpath}", spec["size_bytes"]):
                raise ValueError("rendition upload verification failed")
            await self._check_package(task)
            await self._publish(task, index, spec, relpath)
            produced += 1
            output.unlink()
        return produced

    async def _publish(self, task: RenditionTask, index: dict, spec: dict, relpath: str) -> None:
        if not await self.port.exists(f"{safe_path(task.root)}/{safe_path(task.media['path'])}",
                                      int(task.media["size_bytes"])):
            raise ValueError("source removed before index publication")
        parent, height = task.media["sha256"], spec["height"]
        entry = {**spec, "path": relpath, "kind": "video", "mime_type": "video/mp4",
                 "container": "mp4", "codec": "h264", "variant_of": parent,
                 "resolution_label": f"{height}p"}
        index["media"] = [e for e in index["media"]
                          if not (e.get("variant_of") == parent and e.get("height") == height)]
        index["media"].append(entry)
        # Publish each verified output before starting the next encode.
        await self.port.write_json(f"{safe_path(task.root)}/{INDEX}", index)

    async def _check_package(self, task: RenditionTask) -> None:
        root = safe_path(task.root)
        manifest = await self.port.read_json(f"{root}/manifest.json")
        complete = await self.port.read_json(f"{root}/_COMPLETE.json")
        if (not isinstance(manifest, dict) or not isinstance(complete, dict)
                or hashlib.sha256(canonical(manifest)).hexdigest() != task.manifest_hash
                or manifest.get("package_id") != task.package_id
                or complete.get("package_id") != task.package_id
                or complete.get("manifest_sha256") != task.manifest_hash
                or task.media not in manifest.get("media", [])):
            raise ValueError("committed package changed")
