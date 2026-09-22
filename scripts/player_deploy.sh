#!/usr/bin/env bash
set -Eeuo pipefail

# Player-only cutover. It never invokes the Bot compose file or service.
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
compose_file="$repo_root/docker-compose.player.yml"

usage() {
  printf 'usage: %s --env-file PLAYER_ENV --execute\n' "$0" >&2
  exit 2
}

env_file=
execute=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) env_file=${2:-}; shift 2 ;;
    --execute) execute=true; shift ;;
    *) usage ;;
  esac
done
[[ "$execute" == true && -n "$env_file" ]] || usage
[[ -f "$env_file" && ! -L "$env_file" ]] || { printf 'Player env file is missing or a symlink\n' >&2; exit 2; }
[[ "$(stat -c '%a' "$env_file")" == 600 ]] || { printf 'Player env file must have mode 0600\n' >&2; exit 2; }
[[ "$env_file" != "$repo_root/.env" ]] || { printf 'refusing the Bot environment file\n' >&2; exit 2; }
grep -qx 'TGVIO_PLAYER_ENABLED=true' "$env_file" || {
  printf 'Player env file must explicitly set TGVIO_PLAYER_ENABLED=true\n' >&2
  exit 2
}

forbidden='^(BOT_TOKEN|API_ID|API_HASH|DEST_CHANNEL|ALLOWED_USERS|TGVIO_(DATA|DOWNLOAD|SESSION|LOG)_DIR)='
if grep -Eq "$forbidden" "$env_file"; then
  printf 'Player env file contains a Bot-only variable\n' >&2
  exit 2
fi

bot_before=$(docker inspect --format '{{.Id}} {{.State.Status}}' tgvio 2>/dev/null) || {
  printf 'refusing Player deployment: Bot container tgvio is not inspectable\n' >&2
  exit 2
}
[[ "$bot_before" == *' running' ]] || { printf 'refusing Player deployment: Bot is not running\n' >&2; exit 2; }

docker compose --project-name tgvio-player --env-file "$env_file" -f "$compose_file" --profile player config --quiet
docker compose --project-name tgvio-player --env-file "$env_file" -f "$compose_file" --profile player \
  up -d --no-build --pull never --force-recreate --no-deps tgvio-player

bot_after=$(docker inspect --format '{{.Id}} {{.State.Status}}' tgvio 2>/dev/null) || {
  printf 'Player deployment changed Bot container visibility\n' >&2
  exit 1
}
[[ "$bot_after" == "$bot_before" ]] || {
  printf 'Player deployment changed the Bot container; investigate before further actions\n' >&2
  exit 1
}
printf 'player_deploy=started bot_container_unchanged=true\n'
