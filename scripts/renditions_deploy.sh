#!/usr/bin/env bash
# Start only the explicitly authorized maintenance worker, never a Telegram Bot.
set -Eeuo pipefail
image= commit= env_file= work_dir=
while [[ $# -gt 0 ]]; do
  case "$1" in
    --image) image=$2; shift 2;;
    --commit) commit=$2; shift 2;;
    --env-file) env_file=$2; shift 2;;
    --source-dir)$2; shift 2;;
    --work-dir) work_dir=$2; shift 2;;
    *) printf 'invalid argument\n' >&2; exit 2;;
  esac
done
[[ "$commit" =~ ^[0-9a-f]{40}$ && -n "$image" ]]
[[ -f "$env_file" && -d "$work_dir" ]]
[[ "$(stat -c %a "$env_file")" == 600 ]]
[[ "$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image")" == "$commit" ]]
# Reject an env file that could give the worker a Telegram identity.
if grep -Eiq '(^|_)(BOT_TOKEN|SESSION|API_ID|API_HASH)=' "$env_file"; then
  printf 'maintenance env contains identity fields\n' >&2; exit 2
fi
before=$(docker inspect --format '{{.Id}} {{.RestartCount}}' tgvio)
if docker inspect tgvio-renditions >/dev/null 2>&1; then
  printf 'worker already exists; inspect it before a separate update\n' >&2; exit 2
fi
docker run -d --name tgvio-renditions --restart unless-stopped \
  --cpus 1.5 --memory 2g --pids-limit 96 \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=128m \
  --env-file "$env_file" \
  --mount "type=bind,src=$work_dir,dst=/work" \
  --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
  "$image" --watch
[[ "$before" == "$(docker inspect --format '{{.Id}} {{.RestartCount}}' tgvio)" ]]
printf 'rendition_worker=started bot_container_unchanged=true\n'
