#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"
python_bin=${PYTHON_BIN:-python3}
command -v "$python_bin" >/dev/null 2>&1 || {
  printf 'Python interpreter is missing: %s\n' "$python_bin" >&2
  exit 2
}

export PYTHONPATH="$repo_root/src:/opt/tgvio/site-packages"

# The foundation probe is deliberately isolated from any caller environment.
# It creates a throw-away SQLite database and can never connect to Telegram.
check_root=$(mktemp -d "${TMPDIR:-/tmp}/tgvio-foundation.XXXXXX")
export TGVIO_ENV=test
export TGVIO_RUN_BOT=false
export TGVIO_PUBLISH_ENABLED=false
export TGVIO_LIVE_FIXTURE_ENABLED=false
export TGVIO_ARCHIVE_ENABLED=false
export TGVIO_URL_ENABLED=false
export TGVIO_LOG_FILE_ENABLED=false
export TGVIO_DATA_DIR="$check_root/data"
export TGVIO_DOWNLOAD_DIR="$check_root/downloads"
export TGVIO_LOG_DIR="$check_root/logs"
export API_ID=1
export API_HASH=foundation-placeholder
export BOT_TOKEN=foundation-placeholder
export DEST_CHANNEL=@foundation_placeholder
export ALLOWED_USERS=1

"$python_bin" scripts/release_guard.py verify-tree "$repo_root"
"$python_bin" scripts/release_guard.py architecture "$repo_root"
"$python_bin" -m compileall -q src tests scripts
"$python_bin" -m tgvio.main --check
"$python_bin" -m unittest discover -s tests -v
printf 'foundation_gates=passed\n'
