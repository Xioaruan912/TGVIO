#!/usr/bin/env python3
"""Inspect a candidate runtime image without starting the Bot."""

from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import distributions
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys


EXPECTED_PACKAGES = {
    "aiosqlite": "0.22.1",
    "cryptg": "0.6.0",
    "pyaes": "1.6.1",
    "pyasn1": "0.6.4",
    "rsa": "4.9.1",
    "telethon": "1.44.0",
    "yt-dlp": "2026.8.19",
}
FORBIDDEN_PATHS = (
    ".env",
    ".git",
    "data",
    "downloads",
    "logs",
    "session",
    "tests",
)
SECRET_PATTERNS = (
    re.compile(br"-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----"),
    re.compile(br"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(br"(?<![A-Za-z0-9])\d{7,12}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9])"),
)


def source_manifest(root: Path) -> str:
    files = sorted(
        (path for path in (root / "src").rglob("*.py") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix().encode("utf-8"),
    )
    if not files:
        raise RuntimeError("runtime image has no Python sources")
    combined = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix()
        combined.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
        combined.update(b"  ")
        combined.update(relative.encode("utf-8"))
        combined.update(b"\n")
    return combined.hexdigest()


def inspect_image(root: Path, expected_manifest: str | None) -> dict[str, object]:
    root = root.resolve()
    problems: list[str] = []
    for relative in FORBIDDEN_PATHS:
        if (root / relative).exists():
            problems.append(f"forbidden runtime path: /app/{relative}")
    for required in ("src/tgvio/main.py", "scripts/healthcheck.py", "scripts/image_inspect.py"):
        if not (root / required).is_file():
            problems.append(f"missing runtime path: /app/{required}")

    project_files = 0
    for directory, names, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in names:
            path = directory_path / name
            if name == "__pycache__":
                problems.append(f"Python cache directory in runtime project: {path.relative_to(root)}")
            if stat.S_ISLNK(path.lstat().st_mode):
                problems.append(f"symbolic link in runtime project: {path.relative_to(root)}")
        for name in filenames:
            path = directory_path / name
            if path.suffix in {".pyc", ".pyo"}:
                problems.append(f"Python bytecode in runtime project: {path.relative_to(root)}")
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                problems.append(f"non-regular runtime project file: {path.relative_to(root)}")
                continue
            project_files += 1
            if path.stat().st_size <= 5 * 1024 * 1024:
                content = path.read_bytes()
                if b"\x00" not in content and any(pattern.search(content) for pattern in SECRET_PATTERNS):
                    problems.append(f"credential pattern in runtime project: {path.relative_to(root)}")

    manifest = source_manifest(root)
    if expected_manifest and manifest != expected_manifest:
        problems.append("runtime source manifest does not match release manifest")

    package_path = "/opt/tgvio/site-packages"
    installed = {
        (distribution.metadata.get("Name") or "").lower(): distribution.version
        for distribution in distributions(path=[package_path])
    }
    if installed != EXPECTED_PACKAGES:
        problems.append(
            "locked dependency set mismatch: "
            + json.dumps(installed, sort_keys=True, separators=(",", ":"))
        )
    build_tools = [tool for tool in ("gcc", "g++", "make") if shutil.which(tool)]
    if build_tools:
        problems.append("build tools present in runtime image: " + ",".join(build_tools))
    if problems:
        raise RuntimeError("; ".join(problems))
    return {
        "status": "passed",
        "source_manifest": manifest,
        "project_files": project_files,
        "locked_packages": installed,
        "build_tools": build_tools,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/app"))
    parser.add_argument("--expect-source-manifest")
    parser.add_argument("--print-source-manifest", action="store_true")
    args = parser.parse_args()
    try:
        if args.print_source_manifest:
            print(source_manifest(args.root))
            return 0
        print(
            json.dumps(
                inspect_image(args.root, args.expect_source_manifest),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (OSError, RuntimeError) as exc:
        print(f"runtime image inspection failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
