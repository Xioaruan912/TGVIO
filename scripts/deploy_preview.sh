#!/bin/sh
set -eu

echo "TGVIO deploy preview only."
echo "Production cutover is intentionally disabled in Phase 0."
echo "Target: ${VPS_USER:-root}@${VPS_HOST:-199.47.242.40}:${VPS_APP_DIR:-/root/TGVIO}"
echo "When cutover is approved, deployment will reuse VPS_SSH_KEY without copying the private key into Git."

