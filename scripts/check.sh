#!/usr/bin/env bash
# Local verification only; never deploys or starts the Bot. The only install is the
# pinned front end's npm dependencies inside a temporary export.
set -Eeuo pipefail
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"
browser=false
if [[ ${1:-} == --browser && $# == 1 ]]; then
    browser=true
elif [[ $# != 0 ]]; then
    printf 'usage: %s [--browser]\n' "$0" >&2
    exit 2
fi
python_bin=${PYTHON_BIN:-}
if [[ -z "$python_bin" ]]; then
    if [[ -x "$repo_root/.venv/bin/python" ]]; then
        python_bin="$repo_root/.venv/bin/python"
    else
        python_bin=python3
    fi
fi
command -v "$python_bin" >/dev/null || { printf 'Missing Python: %s\n' "$python_bin" >&2; exit 2; }
command -v npm >/dev/null || { printf 'Missing npm\n' >&2; exit 2; }
export PYTHONPATH="$repo_root/src:${PYTHONPATH:-}"
export PYTHONDONTWRITEBYTECODE=1
"$python_bin" scripts/repository_hygiene.py
"$python_bin" scripts/release_guard.py architecture .
"$python_bin" -m compileall -q src tests scripts
"$python_bin" -m unittest discover -s tests
# The Player front end is checked as the exact commit pinned in player-web.lock,
# installed into a temporary export that is removed on exit.
web_source=$(mktemp -d "${TMPDIR:-/tmp}/tgvio-player-web.XXXXXX")
trap 'bash "$repo_root/scripts/player_web_source.sh" --remove "$web_source"' EXIT
web_commit=$(bash scripts/player_web_source.sh --dest "$web_source")
npm --prefix "$web_source" ci --no-audit --no-fund
npm --prefix "$web_source" run check
if [[ "$browser" == true ]]; then
    npm --prefix "$web_source" run test:browser
fi
git diff --check
printf 'project_checks=passed python=%s browser=%s player_web=%s\n' "$("$python_bin" --version)" "$browser" "$web_commit"
