#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
log_file=${TGVIO_LOG_FILE:-}
if [ -z "$log_file" ]; then
  if [ -f /root/TGVIO/logs/tgvio.jsonl ]; then
    log_file=/root/TGVIO/logs/tgvio.jsonl
  else
    log_file="$repo_root/logs/tgvio.jsonl"
  fi
fi

exec python3 "$repo_root/scripts/logs.py" --file "$log_file" "$@"
