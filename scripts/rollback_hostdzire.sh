#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
deploy_key=${TGVIO_DEPLOY_KEY:-/root/.ssh/tgvio_hostdzire_ed25519}
known_hosts="$repo_root/deploy/hostdzire_known_hosts"
expected_host_fingerprint='SHA256:1QFIKfh+MeSYTGuU/8PsGjlGfDDzVrJXqT+ilmSfqAw'

usage() {
  printf 'usage: %s --check RELEASE_ID\n' "$0" >&2
  printf '       %s --execute RELEASE_ID [--restore-db]\n' "$0" >&2
  exit 2
}

[[ $# -ge 2 && $# -le 3 ]] || usage
mode=$1
release_id=$2
restore=${3:-}
[[ "$release_id" =~ ^r2-[0-9]{2}([a-z][0-9]*)?-[0-9a-f]{7}-[0-9]{8}T[0-9]{6}Z$ ]] || {
  printf 'invalid release id\n' >&2
  exit 2
}
[[ -f "$deploy_key" && ! -L "$deploy_key" ]] || {
  printf 'dedicated HostDZire key is missing\n' >&2
  exit 2
}
[[ -f "$known_hosts" && ! -L "$known_hosts" ]] || {
  printf 'pinned HostDZire known_hosts file is missing\n' >&2
  exit 2
}
known_host_count=$(awk 'NF && $1 !~ /^#/ {count++} END {print count+0}' "$known_hosts")
known_host_entry=$(awk 'NF && $1 !~ /^#/ {print}' "$known_hosts")
[[ "$known_host_count" == 1 && "$known_host_entry" == HostDZire\ ssh-ed25519\ * ]] || {
  printf 'HostDZire known_hosts must contain exactly one ED25519 key\n' >&2
  exit 2
}
key_mode=$(stat -c '%a' "$deploy_key")
(( (8#$key_mode & 077) == 0 )) || {
  printf 'dedicated HostDZire key permissions are too broad\n' >&2
  exit 2
}
host_fingerprint=$(ssh-keygen -lf "$known_hosts")
[[ "$host_fingerprint" == *"$expected_host_fingerprint"* ]] || {
  printf 'HostDZire host-key fingerprint mismatch\n' >&2
  exit 2
}

case "$mode" in
  --check)
    [[ -z "$restore" ]] || usage
    remote_action=rollback-check
    ;;
  --execute)
    [[ -z "$restore" || "$restore" == --restore-db ]] || usage
    remote_action=rollback
    ;;
  *) usage ;;
esac

remote_script="/root/TGVIO-releases/${release_id}/source/scripts/remote_release.sh"
remote_command=$(printf '%q ' "$remote_script" "$remote_action" "$release_id")
if [[ -n "$restore" ]]; then
  remote_command+=" $(printf '%q' "$restore")"
fi

exec ssh \
  -i "$deploy_key" \
  -p 22 \
  -o IdentitiesOnly=yes \
  -o BatchMode=yes \
  -o StrictHostKeyChecking=yes \
  -o "UserKnownHostsFile=$known_hosts" \
  -o HostKeyAlias=HostDZire \
  root@199.47.242.40 \
  "$remote_command"
