#!/usr/bin/env bash
# A separate cover maintenance writer; no Bot/Player DB or rendition mounts.
set -Eeuo pipefail
image= commit= env_file= work_dir=
while [[ $# -gt 0 ]]; do
  case "$1" in
    --image) image=$2; shift 2;;
    --commit) commit=$2; shift 2;;
    --env-file) env_file=$2; shift 2;;
    --work-dir) work_dir=$2; shift 2;;
    *) printf 'invalid argument\n' >&2; exit 2;;
  esac
done
[[ "$commit" =~ ^[0-9a-f]{40}$ && -n "$image" ]]
[[ -f "$env_file" && ! -L "$env_file" && -d "$work_dir" && ! -L "$work_dir" ]]
[[ "$(stat -c %a "$env_file")" == 600 ]]
[[ "$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image")" == "$commit" ]]
# The maintenance capability has exactly these archive fields, no identity.
if grep -Ev '^(TGVIO_ARCHIVE_(WEBDAV_URL|WEBDAV_USER|WEBDAV_PASSWORD|REMOTE_ROOT)=|$|#)' "$env_file" >/dev/null; then
  printf 'unexpected maintenance environment field\n' >&2; exit 2
fi
before_bot=$(docker inspect --format '{{.Id}} {{.RestartCount}}' tgvio)
before_player=$(docker inspect --format '{{.Id}} {{.RestartCount}}' tgvio-player)
before_renditions=$(docker inspect --format '{{.Id}} {{.RestartCount}}' tgvio-renditions)
if docker inspect tgvio-covers >/dev/null 2>&1; then
  printf 'cover worker already exists; inspect it before a separate update\n' >&2; exit 2
fi
docker run -d --name tgvio-covers --restart unless-stopped \
  --cpus 0.75 --memory 256m --pids-limit 64 \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --env-file "$env_file" \
  --mount "type=bind,src=$work_dir,dst=/work" \
  --log-driver json-file --log-opt max-size=5m --log-opt max-file=3 \
  --entrypoint python "$image" -m tgvio.interfaces.backfill_covers --watch --priority-file /work/priorities.json
[[ "$before_bot" == "$(docker inspect --format '{{.Id}} {{.RestartCount}}' tgvio)" ]]
[[ "$before_player" == "$(docker inspect --format '{{.Id}} {{.RestartCount}}' tgvio-player)" ]]
[[ "$before_renditions" == "$(docker inspect --format '{{.Id}} {{.RestartCount}}' tgvio-renditions)" ]]
printf 'cover_worker=started existing_containers_unchanged=true\n'
