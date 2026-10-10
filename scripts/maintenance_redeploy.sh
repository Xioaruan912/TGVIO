#!/usr/bin/env bash
# Replace tgvio-renditions / tgvio-covers with a build of one pushed TGVIO commit,
# one after the other so their cold first scans do not hit the cloud drive at the
# same time. Each old container is stopped and kept, renamed, as rollback; each
# checkpoint database is copied first.
# Usage: bash scripts/maintenance_redeploy.sh [commit]   (default: origin/main)
# Env: TGVIO_MAINTENANCE_ENV (archive env file), TGVIO_RENDITIONS_WORK, TGVIO_COVERS_WORK.
set -Eeuo pipefail

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
env_file=${TGVIO_MAINTENANCE_ENV:-/root/tgvio-renditions/archive.env}
stamp=$(date -u +%Y%m%dT%H%M%SZ)

git -C "$repo" fetch -q origin main
commit=$(git -C "$repo" rev-parse "${1:-origin/main}")
git -C "$repo" merge-base --is-ancestor "$commit" origin/main || { echo "commit is not pushed" >&2; exit 2; }
image=tgvio-maintenance:${commit:0:7}

if ! docker image inspect "$image" >/dev/null 2>&1; then
    src=$(mktemp -d)
    git -C "$repo" archive "$commit" | tar -x -C "$src"
    docker build -q -f "$src/Dockerfile.renditions" --build-arg APP_COMMIT="$commit" -t "$image" "$src"
    rm -r -- "$src"
fi
[[ "$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image")" == "$commit" ]]

replace() {  # name work_dir deploy_script
    local name=$1 work=$2 script=$3
    if [[ "$(docker inspect -f '{{.State.Running}}' "$name")" == true ]]; then
        docker stop -t 60 "$name" >/dev/null
    fi
    cp -a "$work/progress.sqlite3" "$work/progress.sqlite3.bak-$stamp"
    docker rename "$name" "$name-prev-$stamp"
    bash "$repo/scripts/$script" --image "$image" --commit "$commit" --env-file "$env_file" --work-dir "$work"
    echo "$name: started ${commit:0:7} (rollback container $name-prev-$stamp)"
}

wait_scan() {  # name: wait until its first "scan" event, report its cache stats
    local name=$1
    for _ in $(seq 1 120); do
        if docker logs "$name" 2>&1 | grep -q '"event": "scan"'; then
            docker logs "$name" 2>&1 | grep '"event": "scan"' | tail -1
            return 0
        fi
        sleep 15
    done
    echo "$name: no scan event within 30 min" >&2; return 1
}

replace tgvio-renditions "${TGVIO_RENDITIONS_WORK:-/root/tgvio-renditions/work}" renditions_deploy.sh
wait_scan tgvio-renditions
replace tgvio-covers "${TGVIO_COVERS_WORK:-/root/tgvio-covers/work}" covers_deploy.sh
wait_scan tgvio-covers
docker ps --format '{{.Names}}\t{{.Status}}' | grep tgvio
