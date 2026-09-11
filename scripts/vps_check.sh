#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
deploy_key=${TGVIO_DEPLOY_KEY:-/root/.ssh/tgvio_hostdzire_ed25519}
test -f "$deploy_key" || {
  printf 'dedicated HostDZire key is missing: %s\n' "$deploy_key" >&2
  exit 2
}

exec ssh \
  -i "$deploy_key" \
  -p 22 \
  -o IdentitiesOnly=yes \
  -o BatchMode=yes \
  -o StrictHostKeyChecking=yes \
  -o "UserKnownHostsFile=$repo_root/deploy/hostdzire_known_hosts" \
  -o HostKeyAlias=HostDZire \
  root@199.47.242.40 \
  'set -eu; source_root=/root/TGVIO; if test -L /root/TGVIO-current; then source_root=$(readlink -f /root/TGVIO-current); fi; python3 "$source_root/scripts/remote_preflight.py"'
