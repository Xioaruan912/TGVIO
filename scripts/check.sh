#!/usr/bin/env bash
# Local verification only; never installs, deploys or starts the Bot.
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
npm --prefix player/web run check
if [[ "$browser" == true ]]; then
    npm --prefix player/web run test:browser
fi
git diff --check
printf 'project_checks=passed python=%s browser=%s\n' "$("$python_bin" --version)" "$browser"
