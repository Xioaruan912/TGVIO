#!/usr/bin/env bash
set -Eeuo pipefail

# Export the front-end source pinned in player-web.lock into an empty directory.
# The tree comes from the Git object database (git archive), so uncommitted or
# unpushed work in the local clone can never enter a check or an image. Prints
# only the exported commit on stdout.
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

usage() {
  printf 'usage: %s --dest EMPTY_DIR [--lock FILE] [--git CLONE]\n' "$0" >&2
  printf '       %s --remove EXPORT_DIR\n' "$0" >&2
  printf 'CLONE defaults to $PLAYER_WEB_GIT, then %s\n' "$repo_root/../TGVIO-Player" >&2
  printf 'Callers create EXPORT_DIR with: mktemp -d "${TMPDIR:-/tmp}/tgvio-player-web.XXXXXX"\n' >&2
  exit 2
}
fail() { printf 'player_web_source: %s\n' "$1" >&2; exit 1; }

# Remove only a private export directory created by the caller's mktemp.
if [[ ${1:-} == --remove && $# == 2 ]]; then
  target=$2
  parent=$(CDPATH= cd -- "${TMPDIR:-/tmp}" && pwd -P)
  [[ -e "$target" ]] || exit 0
  [[ -d "$target" && ! -L "$target" ]] || fail "refusing unsafe cleanup: $target"
  [[ "$(CDPATH= cd -- "$(dirname -- "$target")" && pwd -P)" == "$parent" ]] || fail "refusing unsafe cleanup: $target"
  [[ "$(basename -- "$target")" == tgvio-player-web.* ]] || fail "refusing unsafe cleanup: $target"
  find "$target" -depth -delete
  exit 0
fi

dest=
lock="$repo_root/player-web.lock"
clone=${PLAYER_WEB_GIT:-$repo_root/../TGVIO-Player}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dest) dest=${2:-}; shift 2 ;;
    --lock) lock=${2:-}; shift 2 ;;
    --git) clone=${2:-}; shift 2 ;;
    *) usage ;;
  esac
done
[[ -n "$dest" ]] || usage

[[ -f "$lock" && ! -L "$lock" ]] || fail "lock file missing or a symlink: $lock"
repository= commit= extra=
while IFS= read -r line || [[ -n "$line" ]]; do
  case "$line" in
    ''|'#'*) ;;
    repository=*) [[ -z "$repository" ]] || fail "duplicate repository in lock"; repository=${line#repository=} ;;
    commit=*) [[ -z "$commit" ]] || fail "duplicate commit in lock"; commit=${line#commit=} ;;
    *) extra=$line ;;
  esac
done < "$lock"
[[ -z "$extra" ]] || fail "unexpected lock line: $extra"
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || fail "lock commit must be a full 40-hex SHA"
[[ "$repository" =~ ^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\.git$ ]] \
  || fail "lock repository must be an https://github.com/OWNER/REPO.git URL"

# ssh and https spellings of the same GitHub repository are the same source.
normalize() {
  local url=${1%.git}
  url=${url#https://github.com/}
  url=${url#ssh://git@github.com/}
  url=${url#git@github.com:}
  printf '%s' "${url,,}"
}

[[ -d "$clone" ]] || fail "front-end clone not found at $clone; run: git clone $repository $clone"
git_web() { git -C "$clone" -c safe.directory="$clone" "$@"; }
[[ "$(git_web rev-parse --is-inside-work-tree 2>/dev/null || git_web rev-parse --is-bare-repository 2>/dev/null)" == true ]] \
  || fail "not a Git repository: $clone"
origin=$(git_web config --get remote.origin.url) || fail "clone has no origin remote: $clone"
[[ "$(normalize "$origin")" == "$(normalize "$repository")" ]] \
  || fail "clone origin $origin does not match locked repository $repository"

# Only a commit already on the published main branch may be built.
published() {
  git_web cat-file -e "$commit^{commit}" 2>/dev/null \
    && git_web rev-parse --verify --quiet refs/remotes/origin/main >/dev/null \
    && git_web merge-base --is-ancestor "$commit" refs/remotes/origin/main
}
if ! published; then
  git_web fetch --quiet origin '+refs/heads/main:refs/remotes/origin/main' \
    || fail "cannot fetch origin main to verify $commit"
  published || fail "locked commit $commit is not on origin/main of $repository"
fi

if [[ -e "$dest" ]]; then
  [[ -d "$dest" && ! -L "$dest" ]] || fail "destination is not a plain directory: $dest"
  [[ -z "$(ls -A -- "$dest")" ]] || fail "destination is not empty: $dest"
else
  mkdir -p -- "$dest"
fi
git_web archive --format=tar "$commit" | tar -x -C "$dest"
[[ -f "$dest/package.json" && -f "$dest/package-lock.json" && -d "$dest/src" ]] \
  || fail "exported tree is not the Player front end"
printf '%s\n' "$commit"
