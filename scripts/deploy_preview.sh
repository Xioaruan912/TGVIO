#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
printf 'deploy_preview.sh is retained as a compatibility entrypoint; running the fail-closed release workflow.\n'
exec python3 "$script_dir/deploy_hostdzire.py" "$@"
