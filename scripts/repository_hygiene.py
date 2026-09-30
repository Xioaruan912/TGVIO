#!/usr/bin/env python3
"""Check versioned governance, source budgets, docs and generated-file hygiene."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

from release_guard import _forbidden_path_reason, _secret_findings
from pathlib import PurePosixPath


REQUIRED = (
    "AGENTS.md", "AI_DEVELOPMENT.md", "player/AGENTS.md",
    "src/tgvio/AGENTS.md", "src/tgvio_player/AGENTS.md",
    "docs/README.md", "docs/development/README.md",
    "docs/development/ARCHITECTURE.md", "docs/operations/README.md",
    "docs/archive/README.md", "docs/refactor-v2/README.md",
    "scripts/check.sh", "scripts/repository_hygiene.py",
    "player/web/tsconfig.test.json",
)
# Existing debt may only shrink. New modules receive no exemption.
FRONTEND_DEBT = {"main.ts": 1710, "large.ts": 770}
FRONTEND_LIMIT = 600
GOVERNANCE_LIMIT = 160
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
DIRECT_TS = re.compile(r"""(?:from\s*|import\s*\()\s*["'][^"']+\.ts["']""")


def generated_path(name: str) -> bool:
    return bool(set(Path(name).parts) & {
        "node_modules", "dist", ".test-dist", "__pycache__", ".venv", ".superpowers",
    }) or name.endswith((".pyc", ".pyo"))


def frontend_problems(root: Path) -> list[str]:
    problems = []
    for path in sorted((root / "player/web/src").rglob("*.ts")):
        relative = path.relative_to(root / "player/web/src").as_posix()
        size = len(path.read_text(encoding="utf-8").splitlines())
        budget = FRONTEND_DEBT.get(relative, FRONTEND_LIMIT)
        if size > budget:
            problems.append(f"{path.relative_to(root)}: {size} lines exceeds {budget}")
    for path in sorted((root / "player/web/tests").glob("*.test.mjs")):
        if DIRECT_TS.search(path.read_text(encoding="utf-8")):
            problems.append(f"{path.relative_to(root)}: Node tests must import compiled JS, not TypeScript")
    return problems


def document_problems(root: Path, names: list[str]) -> list[str]:
    problems = []
    for name in names:
        if not name.endswith(".md"):
            continue
        if name not in REQUIRED and not name.startswith(("docs/development/", "docs/operations/", "docs/handoffs/")):
            continue  # Historical evidence is not the current normative link graph.
        path = root / name
        content = path.read_text(encoding="utf-8")
        if path.name == "AGENTS.md" and len(content.splitlines()) > GOVERNANCE_LIMIT:
            problems.append(f"{name}: governance exceeds {GOVERNANCE_LIMIT} lines; move history to handoffs")
        for match in LINK.finditer(content):
            target = match.group(1).strip().strip("<>").split("#", 1)[0]
            if not target or re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", target):
                continue
            destination = (path.parent / target).resolve()
            if not destination.is_relative_to(root.resolve()) or not destination.exists():
                problems.append(f"{name}: missing or escaping local link {target}")
            elif destination.is_file() and destination.relative_to(root.resolve()).as_posix() not in names:
                problems.append(f"{name}: linked document is not tracked: {target}")
    return problems


def check(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True,
    )
    names = [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]
    problems = [f"required project file is not tracked: {name}" for name in REQUIRED if name not in names]
    for name in names:
        path = root / name
        reason = _forbidden_path_reason(PurePosixPath(name))
        if reason or generated_path(name):
            problems.append(f"{name}: forbidden tracked file ({reason or 'generated/local artifact'})")
        elif path.is_symlink():
            problems.append(f"{name}: tracked symlink")
        elif not path.is_file():
            problems.append(f"{name}: tracked file is missing")
        else:
            for finding in _secret_findings(path.read_bytes()):
                problems.append(f"{name}: {finding}")
    if not any("missing" in item for item in problems):
        problems.extend(document_problems(root, names))
    problems.extend(frontend_problems(root))
    return problems


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    problems = check(root)
    if problems:
        print("repository_hygiene=failed\n" + "\n".join(problems), file=sys.stderr)
        return 1
    print(json.dumps({
        "repository_hygiene": "passed", "frontend_module_limit": FRONTEND_LIMIT,
        "existing_frontend_debt": FRONTEND_DEBT, "governance_line_limit": GOVERNANCE_LIMIT,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
