#!/usr/bin/env bash
set -Eeuo pipefail

# Build a local Player-only candidate. This command never starts or restarts
# containers. An uploaded/built candidate becomes a production release only
# after an authorized cutover and verification of the actual running image.
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

# The front end is the exact commit pinned in player-web.lock, exported from Git
# objects; a missing, unpushed or mismatched source stops the build here.
web_source=$(mktemp -d "${TMPDIR:-/tmp}/tgvio-player-web.XXXXXX")
trap 'bash "$repo_root/scripts/player_web_source.sh" --remove "$web_source"' EXIT
web_commit=$(bash "$repo_root/scripts/player_web_source.sh" --dest "$web_source")

DOCKER_BUILDKIT=1 docker build --pull=false \
  --file Dockerfile.player \
  --build-context "player-web=$web_source" \
  --build-arg "PLAYER_COMMIT=$commit" \
  --build-arg "PLAYER_RELEASE_ID=$release_id" \
  --build-arg "PLAYER_WEB_COMMIT=$web_commit" \
  --tag "$tag" .
printf 'player_release_candidate=%s player_web_commit=%s\n' "$tag" "$web_commit"
