#!/usr/bin/env bash
set -Eeuo pipefail

# Build a local Player-only candidate. This command never starts or restarts
# containers and is not a production release until R2-19B/C provides an HTTP
# composition root and the owner authorizes production deployment.
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"

usage() {
  printf 'usage: %s --tag IMAGE_TAG [--commit COMMIT] [--release-id ID]\n' "$0" >&2
  exit 2
}

tag=
commit=$(git -c safe.directory="$repo_root" rev-parse HEAD 2>/dev/null || printf 'unknown')
release_id=local-player
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag) tag=${2:-}; shift 2 ;;
    --commit) commit=${2:-}; shift 2 ;;
    --release-id) release_id=${2:-}; shift 2 ;;
    *) usage ;;
  esac
done
[[ -n "$tag" && "$tag" != *$'\n'* ]] || usage
[[ "$commit" =~ ^([0-9a-f]{40}|unknown)$ ]] || { printf 'invalid commit\n' >&2; exit 2; }
[[ "$release_id" =~ ^[A-Za-z0-9._-]+$ ]] || { printf 'invalid release id\n' >&2; exit 2; }

DOCKER_BUILDKIT=1 docker build --pull=false \
  --file Dockerfile.player \
  --build-arg "PLAYER_COMMIT=$commit" \
  --build-arg "PLAYER_RELEASE_ID=$release_id" \
  --tag "$tag" .
printf 'player_release_candidate=%s\n' "$tag"
