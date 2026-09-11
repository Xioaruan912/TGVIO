#!/bin/sh
set -eu

exec docker exec tgvio python /app/scripts/logs.py --file /app/logs/tgvio.jsonl "$@"
