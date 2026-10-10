#!/usr/bin/env bash
# Read-only real-Chrome smoke test of the running Player (see player_browser_smoke.py).
# Usage: bash scripts/player_browser_smoke.sh [feed,settings,long,covers]
# Writes report.json and screenshots to a new directory under ${TMPDIR:-/tmp}.
set -Eeuo pipefail
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
image=mcr.microsoft.com/playwright/python:v1.55.0-noble
out=$(mktemp -d "${TMPDIR:-/tmp}/player-smoke.XXXXXX")
secret=$(docker exec tgvio-player printenv TGVIO_PLAYER_ACCESS_SECRET)
PLAYER_SECRET="$secret" docker run --rm --network host --ipc=host \
    -e PLAYER_SECRET -e SMOKE_CHECKS="${1:-feed,settings,long}" \
    -v "$repo/scripts/player_browser_smoke.py:/smoke.py:ro" -v "$out:/out" "$image" \
    bash -c 'pip install -q playwright==1.55.0 >/dev/null 2>&1; playwright install chrome >/dev/null 2>&1 && python /smoke.py'
echo "report: $out/report.json"
