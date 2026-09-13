#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly APP_ROOT=/root/TGVIO
readonly RELEASE_ROOT=/root/TGVIO-releases
readonly CURRENT_LINK=/root/TGVIO-current
readonly CONTAINER=tgvio
readonly SERVICE=tgvio
readonly PROJECT=tgvio
readonly BASE_IMAGE='python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534'

stage=bootstrap
trap 'code=$?; printf "remote_release_failed stage=%s exit=%s\n" "$stage" "$code" >&2' ERR

die() {
  printf 'remote_release_error: %s\n' "$*" >&2
  exit 2
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "missing command: $1"
}

valid_release_id() {
  [[ "$1" =~ ^r2-[0-9]{2}([a-z][0-9]*)?-[0-9a-f]{7}-[0-9]{8}T[0-9]{6}Z$ ]]
}

valid_commit() {
  [[ "$1" =~ ^[0-9a-f]{40}$ ]]
}

valid_sha256() {
  [[ "$1" =~ ^[0-9a-f]{64}$ ]]
}

valid_migration() {
  [[ "$1" == none || "$1" =~ ^[0-9]{4}_[a-z0-9_]+$ ]]
}

json_value() {
  local file=$1
  local field=$2
  python3 - "$file" "$field" <<'PY'
import json
from pathlib import Path
import sys

value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for part in sys.argv[2].split("."):
    if not isinstance(value, dict) or part not in value:
        raise SystemExit(f"missing JSON field: {sys.argv[2]}")
    value = value[part]
if isinstance(value, bool):
    print("true" if value else "false")
elif value is None:
    print("")
elif isinstance(value, (str, int, float)):
    print(value)
else:
    raise SystemExit(f"JSON field is not scalar: {sys.argv[2]}")
PY
}

json_int_list_csv() {
  local file=$1
  local field=$2
  python3 - "$file" "$field" <<'PY'
import json
from pathlib import Path
import sys

value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for part in sys.argv[2].split("."):
    if not isinstance(value, dict) or part not in value:
        raise SystemExit(f"missing JSON field: {sys.argv[2]}")
    value = value[part]
if not isinstance(value, list) or any(isinstance(item, bool) or not isinstance(item, int) for item in value):
    raise SystemExit(f"JSON field is not an integer list: {sys.argv[2]}")
print(",".join(str(item) for item in value))
PY
}

wait_for_health() {
  local deadline=$((SECONDS + 180))
  local state
  while (( SECONDS < deadline )); do
    state=$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CONTAINER" 2>/dev/null || true)
    if [[ "$state" == 'running healthy' ]]; then
      return 0
    fi
    sleep 3
  done
  die "container did not become healthy within 180 seconds"
}

assert_postflight() {
  local report=$1
  [[ "$(json_value "$report" container.instances)" == 1 ]] || die "postflight container count mismatch"
  [[ "$(json_value "$report" container.status)" == running ]] || die "postflight container is not running"
  [[ "$(json_value "$report" container.health)" == healthy ]] || die "postflight health is not healthy"
  [[ "$(json_value "$report" container.restart_count)" == 0 ]] || die "new container restarted"
  [[ "$(json_value "$report" container.error_markers)" == 0 ]] || die "new container logs contain fatal markers"
  (( $(json_value "$report" container.bootstrap_markers) >= 1 )) || die "bootstrap marker missing"
  (( $(json_value "$report" container.telegram_ready_markers) >= 1 )) || die "Telegram ready marker missing"
  [[ "$(json_value "$report" database.quick_check)" == ok ]] || die "postflight SQLite quick_check failed"
}

write_release_env() {
  local output=$1
  local image_ref=$2
  local commit=$3
  local release_id=$4
  local source_manifest=$5
  local lock_sha=$6
  [[ ! -e "$output" ]] || die "release environment already exists"
  python3 - "$output" "$image_ref" "$commit" "$release_id" "$source_manifest" "$lock_sha" <<'PY'
import os
from pathlib import Path
import sys

path = Path(sys.argv[1])
values = {
    "TGVIO_IMAGE": sys.argv[2],
    "APP_COMMIT": sys.argv[3],
    "RELEASE_ID": sys.argv[4],
    "SOURCE_MANIFEST": sys.argv[5],
    "REQUIREMENTS_LOCK_SHA256": sys.argv[6],
    "TGVIO_ENV_FILE": "/root/TGVIO/.env",
    "TGVIO_HOST_DATA_DIR": "/root/TGVIO/data",
    "TGVIO_HOST_DOWNLOAD_DIR": "/root/TGVIO/downloads",
    "TGVIO_HOST_SESSION_DIR": "/root/TGVIO/session",
    "TGVIO_HOST_LOG_DIR": "/root/TGVIO/logs",
}
payload = "".join(f"{key}={value}\n" for key, value in values.items())
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    handle.write(payload)
    handle.flush()
    os.fsync(handle.fileno())
PY
}

atomic_metadata() {
  local source_dir=$1
  local commit=$2
  local release_id=$3
  local link_tmp="/root/.TGVIO-current-${release_id}"
  local commit_tmp="${APP_ROOT}/.release-commit.${release_id}"
  local id_tmp="${APP_ROOT}/.release-id.${release_id}"
  [[ ! -e "$CURRENT_LINK" || -L "$CURRENT_LINK" ]] || die "$CURRENT_LINK is not a symlink"
  [[ ! -e "$link_tmp" && ! -L "$link_tmp" ]] || die "temporary current link already exists"
  [[ ! -e "$commit_tmp" && ! -e "$id_tmp" ]] || die "temporary release metadata already exists"
  ln -s "$source_dir" "$link_tmp"
  printf '%s\n' "$commit" >"$commit_tmp"
  printf '%s\n' "$release_id" >"$id_tmp"
  chmod 600 "$commit_tmp" "$id_tmp"
  mv -Tf "$link_tmp" "$CURRENT_LINK"
  mv -f "$commit_tmp" "${APP_ROOT}/.release-commit"
  mv -f "$id_tmp" "${APP_ROOT}/.release-id"
}

write_previous_json() {
  local output=$1
  local report=$2
  python3 - "$output" "$report" <<'PY'
import json
import os
from pathlib import Path
import sys

report = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
source = str(report["source_root"])
previous_id = str(report.get("release_id") or "legacy-r2-01")
value = {
    "source_root": source,
    "release_id": previous_id,
    "app_commit": str(report["container"]["app_commit"]),
    "image_id": str(report["container"]["image_id"]),
    "source_manifest": str(report["source_manifest"]),
    "compose_kind": "versioned" if (Path(source) / ".release.env").is_file() else "legacy",
}
path = Path(sys.argv[1])
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(value, handle, sort_keys=True, indent=2)
    handle.write("\n")
PY
}

write_backup_json() {
  local output=$1
  local database_json=$2
  local source_archive=$3
  local environment_backup=$4
  local rollback_tag=$5
  python3 - "$output" "$database_json" "$source_archive" "$environment_backup" "$rollback_tag" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import sys

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

database = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
source = Path(sys.argv[3])
environment = Path(sys.argv[4])
value = {
    "database": database,
    "source": {"path": str(source), "sha256": sha256(source), "mode": f"{source.stat().st_mode & 0o777:03o}"},
    "environment": {"path": str(environment), "mode": f"{environment.stat().st_mode & 0o777:03o}"},
    "rollback_image_tag": sys.argv[5],
}
path = Path(sys.argv[1])
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(value, handle, sort_keys=True, indent=2)
    handle.write("\n")
PY
}

write_manifest() {
  local status=$1
  local postflight=${2:-}
  local -a command=(
    python3 "$source_dir/scripts/write_release_manifest.py"
    --manifest-output "$evidence_dir/release-manifest.json"
    --ledger-output "$evidence_dir/deployment-ledger.json"
    --status "$status"
    --release-id "$release_id"
    --git-commit "$commit"
    --git-archive-sha256 "$archive_sha"
    --source-manifest "$expected_source_manifest"
    --requirements-lock-sha256 "$lock_sha"
    --dockerfile-sha256 "$dockerfile_sha"
    --base-image "$BASE_IMAGE"
    --test-image-id "$test_image_id"
    --runtime-image-id "$runtime_image_id"
    --tests "$test_count"
    --test-seconds "$test_seconds"
    --migration "$migration_spec"
    --preflight "$preflight_backup"
    --image-inspection "$evidence_dir/image-inspection.json"
    --backup "$evidence_dir/backup.json"
    --previous "$evidence_dir/previous.json"
  )
  if [[ -n "$postflight" ]]; then
    command+=(--postflight "$postflight")
  fi
  "${command[@]}"
}

deploy_release() {
  [[ $# -eq 9 ]] || die "deploy expects 9 arguments"
  release_id=$1
  commit=$2
  expected_source_manifest=$3
  archive_sha=$4
  lock_sha=$5
  dockerfile_sha=$6
  expected_current_commit=$7
  expected_current_manifest=$8
  migration_spec=$9
  valid_release_id "$release_id" || die "invalid release id"
  valid_commit "$commit" || die "invalid release commit"
  valid_commit "$expected_current_commit" || die "invalid current commit"
  valid_sha256 "$expected_source_manifest" || die "invalid source manifest"
  valid_sha256 "$expected_current_manifest" || die "invalid current source manifest"
  valid_sha256 "$archive_sha" || die "invalid archive hash"
  valid_sha256 "$lock_sha" || die "invalid lock hash"
  valid_sha256 "$dockerfile_sha" || die "invalid Dockerfile hash"
  valid_migration "$migration_spec" || die "invalid migration declaration"

  release_dir="${RELEASE_ROOT}/${release_id}"
  source_dir="${release_dir}/source"
  evidence_dir="${release_dir}/evidence"
  rollback_dir="${release_dir}/rollback"
  incoming_archive="${release_dir}/incoming/source.tar.gz"
  [[ "$(realpath -e "$source_dir")" == "$source_dir" ]] || die "release source path mismatch"
  [[ -f "$incoming_archive" ]] || die "release archive is missing"
  [[ "$(sha256sum "$incoming_archive" | cut -d' ' -f1)" == "$archive_sha" ]] || die "release archive hash mismatch"
  [[ "$(sha256sum "$source_dir/requirements.lock" | cut -d' ' -f1)" == "$lock_sha" ]] || die "requirements lock hash mismatch"
  [[ "$(sha256sum "$source_dir/Dockerfile" | cut -d' ' -f1)" == "$dockerfile_sha" ]] || die "Dockerfile hash mismatch"
  mkdir -p "$evidence_dir" "$rollback_dir"

  stage=source-gates
  python3 "$source_dir/scripts/release_guard.py" verify-archive "$incoming_archive" >"$evidence_dir/archive-guard.json"
  python3 "$source_dir/scripts/release_guard.py" verify-tree "$source_dir" >"$evidence_dir/source-guard.json"
  python3 "$source_dir/scripts/release_guard.py" architecture "$source_dir" >"$evidence_dir/architecture.json"
  [[ "$(python3 "$source_dir/scripts/release_guard.py" source-manifest "$source_dir")" == "$expected_source_manifest" ]] || die "source manifest mismatch"
  docker compose --env-file "$source_dir/deploy/compose.placeholder.env" -f "$source_dir/docker-compose.yml" config --quiet

  stage=preflight-before-build
  preflight_build="$evidence_dir/preflight-before-build.json"
  python3 "$source_dir/scripts/remote_preflight.py" \
    --require-safe \
    --expect-app-commit "$expected_current_commit" \
    --expect-source-manifest "$expected_current_manifest" >"$preflight_build"
  (( $(json_value "$preflight_build" disk.free_bytes) >= 4294967296 )) || die "less than 4 GiB free before build"

  stage=test-image-build
  test_tag="tgvio-test:${release_id}"
  common_build_args=(
    --build-arg "APP_COMMIT=${commit}"
    --build-arg "RELEASE_ID=${release_id}"
    --build-arg "SOURCE_MANIFEST=${expected_source_manifest}"
    --build-arg "REQUIREMENTS_LOCK_SHA256=${lock_sha}"
  )
  DOCKER_BUILDKIT=1 docker build --pull=false --progress=plain \
    --target test \
    --tag "$test_tag" \
    "${common_build_args[@]}" \
    "$source_dir" 2>&1 | tee "$evidence_dir/test-build.log"

  stage=offline-tests
  test_container="tgvio-test-${release_id}"
  docker run --rm --network none --name "$test_container" "$test_tag" \
    2>&1 | tee "$evidence_dir/test.log"
  test_summary=$(sed -nE 's/^Ran ([0-9]+) tests in ([0-9.]+)s$/\1 \2/p' "$evidence_dir/test.log" | tail -n 1)
  [[ -n "$test_summary" ]] || die "unable to parse unittest summary"
  read -r test_count test_seconds <<<"$test_summary"
  (( test_count >= 156 )) || die "test count regressed below the R2-02 release baseline"
  test_image_id=$(docker image inspect --format '{{.Id}}' "$test_tag")

  stage=runtime-image-build
  runtime_tag="tgvio-release:${release_id}"
  DOCKER_BUILDKIT=1 docker build --pull=false --progress=plain \
    --target runtime \
    --tag "$runtime_tag" \
    "${common_build_args[@]}" \
    "$source_dir" 2>&1 | tee "$evidence_dir/runtime-build.log"
  runtime_image_id=$(docker image inspect --format '{{.Id}}' "$runtime_tag")
  [[ "$runtime_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || die "invalid runtime image id"
  [[ "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$runtime_tag")" == linux/amd64 ]] || die "runtime image platform is not linux/amd64"
  [[ "$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$runtime_tag")" == "$commit" ]] || die "runtime image commit label mismatch"
  [[ "$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' "$runtime_tag")" == "$release_id" ]] || die "runtime image release label mismatch"
  [[ "$(docker image inspect --format '{{index .Config.Labels "io.tgvio.source-manifest"}}' "$runtime_tag")" == "$expected_source_manifest" ]] || die "runtime image source label mismatch"
  [[ "$(docker image inspect --format '{{index .Config.Labels "io.tgvio.requirements-lock-sha256"}}' "$runtime_tag")" == "$lock_sha" ]] || die "runtime image lock label mismatch"
  image_ref=$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$runtime_tag" | sed -n '/^tgvio-release@sha256:/p' | head -n 1)
  [[ "$image_ref" =~ ^tgvio-release@sha256:[0-9a-f]{64}$ ]] || die "runtime image has no immutable repository digest"

  stage=runtime-image-inspection
  docker run --rm --network none --entrypoint python "$image_ref" \
    scripts/image_inspect.py --expect-source-manifest "$expected_source_manifest" \
    >"$evidence_dir/image-inspection.json"

  stage=preflight-before-backup
  preflight_backup="$evidence_dir/preflight-before-backup.json"
  python3 "$source_dir/scripts/remote_preflight.py" \
    --require-safe \
    --expect-app-commit "$expected_current_commit" \
    --expect-source-manifest "$expected_current_manifest" >"$preflight_backup"

  stage=rollback-points
  current_source=$(json_value "$preflight_backup" source_root)
  previous_image=$(json_value "$preflight_backup" container.image_id)
  [[ "$previous_image" =~ ^sha256:[0-9a-f]{64}$ ]] || die "invalid previous image id"
  rollback_tag="tgvio-rollback-pre:${release_id}"
  docker image tag "$previous_image" "$rollback_tag"
  python3 "$source_dir/scripts/release_guard.py" sqlite-backup \
    "$APP_ROOT/data/state.sqlite3" "$rollback_dir/state-pre.sqlite3" \
    >"$evidence_dir/database-backup.json"
  tar --sort=name --owner=0 --group=0 --numeric-owner \
    --exclude='./.env' --exclude='./.release.env' --exclude='./.git' \
    --exclude='./data' --exclude='./downloads' --exclude='./session' --exclude='./logs' \
    --exclude='*/__pycache__' --exclude='*.pyc' \
    -C "$current_source" -czf "$rollback_dir/source-pre.tar.gz" .
  chmod 600 "$rollback_dir/source-pre.tar.gz"
  python3 "$source_dir/scripts/release_guard.py" verify-archive "$rollback_dir/source-pre.tar.gz" \
    >"$evidence_dir/source-backup-guard.json"
  install -m 600 "$APP_ROOT/.env" "$rollback_dir/env-pre.bak"
  write_previous_json "$evidence_dir/previous.json" "$preflight_backup"
  write_backup_json "$evidence_dir/backup.json" "$evidence_dir/database-backup.json" \
    "$rollback_dir/source-pre.tar.gz" "$rollback_dir/env-pre.bak" "$rollback_tag"

  if [[ "$migration_spec" != none ]]; then
    stage=migration-rehearsal
    rehearsal_dir="$release_dir/rehearsal"
    rehearsal_report="$evidence_dir/migration-rehearsal.json"
    expected_version=$((10#${migration_spec%%_*}))
    (
      trap 'find "$rehearsal_dir" -depth -delete 2>/dev/null || true' EXIT
      mkdir -p "$rehearsal_dir/backups"
      python3 "$source_dir/scripts/release_guard.py" sqlite-backup \
        "$rollback_dir/state-pre.sqlite3" "$rehearsal_dir/state-copy.sqlite3" \
        >"$evidence_dir/migration-rehearsal-copy.json"
      PYTHONPATH="$source_dir/src" python3 "$source_dir/scripts/rehearse_migration.py" \
        "$rehearsal_dir/state-copy.sqlite3" \
        --backup-dir "$rehearsal_dir/backups" >"$rehearsal_report" \
        2> >(tee "$evidence_dir/migration-rehearsal.stderr.log" >&2)
      [[ "$(json_value "$rehearsal_report" status)" == passed ]] || die "migration rehearsal did not pass"
      [[ "$(json_value "$rehearsal_report" migration.from_version)" == "$(json_value "$preflight_backup" database.user_version)" ]] || die "migration rehearsal source version differs from production"
      [[ "$(json_value "$rehearsal_report" migration.to_version)" == "$expected_version" ]] || die "migration rehearsal target version differs from declared migration"
      [[ "$(json_int_list_csv "$rehearsal_report" migration.applied_now)" == "$expected_version" ]] || die "migration rehearsal applied set differs from declared migration"
      [[ "$(json_value "$rehearsal_report" before.schema_sql_sha256)" == "$(json_value "$preflight_backup" database.schema_sql_sha256)" ]] || die "migration rehearsal source schema differs from production"
      [[ "$(json_value "$rehearsal_report" backup.schema_sql_sha256)" == "$(json_value "$preflight_backup" database.schema_sql_sha256)" ]] || die "migration rehearsal backup does not preserve production schema"
    )
  fi

  stage=release-compose-config
  write_release_env "$source_dir/.release.env" "$image_ref" "$commit" "$release_id" \
    "$expected_source_manifest" "$lock_sha"
  docker compose --env-file "$source_dir/.release.env" -f "$source_dir/docker-compose.yml" config --quiet
  write_manifest ready

  stage=preflight-before-cutover
  preflight_cutover="$evidence_dir/preflight-before-cutover.json"
  python3 "$source_dir/scripts/remote_preflight.py" \
    --require-safe \
    --expect-app-commit "$expected_current_commit" \
    --expect-source-manifest "$expected_current_manifest" >"$preflight_cutover"
  [[ "$(json_value "$preflight_cutover" database.schema_sql_sha256)" == "$(json_value "$preflight_backup" database.schema_sql_sha256)" ]] || die "schema changed during build window"

  stage=cutover
  docker compose --project-name "$PROJECT" --env-file "$source_dir/.release.env" \
    -f "$source_dir/docker-compose.yml" up -d --no-build --pull never \
    --force-recreate --no-deps "$SERVICE"
  wait_for_health

  stage=identity-postflight
  identity_postflight="$evidence_dir/postflight-before-metadata.json"
  python3 "$source_dir/scripts/remote_preflight.py" \
    --source-root "$source_dir" \
    --expect-app-commit "$commit" \
    --expect-source-manifest "$expected_source_manifest" \
    --expect-image "$runtime_image_id" >"$identity_postflight"
  assert_postflight "$identity_postflight"
  if [[ "$migration_spec" == none ]]; then
    [[ "$(json_value "$identity_postflight" database.schema_sql_sha256)" == "$(json_value "$preflight_backup" database.schema_sql_sha256)" ]] || die "schema changed in migration-free release"
    [[ "$(json_value "$identity_postflight" database.user_version)" == "$(json_value "$preflight_backup" database.user_version)" ]] || die "user_version changed in migration-free release"
  else
    before_version=$(json_value "$preflight_backup" database.user_version)
    after_version=$(json_value "$identity_postflight" database.user_version)
    expected_version=$((10#${migration_spec%%_*}))
    [[ "$(json_value "$identity_postflight" database.migration_ledger_present)" == true ]] || die "migration ledger missing after schema-changing release"
    [[ "$after_version" == "$expected_version" ]] || die "postflight user_version does not match declared migration"
    (( after_version > before_version )) || die "declared migration did not advance user_version"
    [[ "$(json_value "$identity_postflight" database.schema_sql_sha256)" != "$(json_value "$preflight_backup" database.schema_sql_sha256)" ]] || die "declared migration did not change schema identity"
  fi

  stage=atomic-release-metadata
  atomic_metadata "$source_dir" "$commit" "$release_id"

  stage=final-postflight
  final_postflight="$evidence_dir/postflight.json"
  python3 "$source_dir/scripts/remote_preflight.py" \
    --expect-app-commit "$commit" \
    --expect-source-manifest "$expected_source_manifest" \
    --expect-image "$runtime_image_id" >"$final_postflight"
  assert_postflight "$final_postflight"
  if [[ "$migration_spec" != none ]]; then
    [[ "$(json_value "$final_postflight" database.migration_ledger_present)" == true ]] || die "final migration ledger missing"
    [[ "$(json_value "$final_postflight" database.user_version)" == "$expected_version" ]] || die "final user_version changed after migration postflight"
  fi
  [[ "$(json_value "$final_postflight" release_commit)" == "$commit" ]] || die "release commit metadata mismatch"
  [[ "$(json_value "$final_postflight" release_id)" == "$release_id" ]] || die "release id metadata mismatch"
  [[ "$(json_value "$final_postflight" safe_to_deploy)" == true ]] || die "final production report is not safe"
  write_manifest deployed "$final_postflight"

  stage=complete
  python3 - "$evidence_dir/release-manifest.json" <<'PY'
import json
from pathlib import Path
import sys

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(json.dumps({
    "status": manifest["status"],
    "release_id": manifest["release_id"],
    "git_commit": manifest["git_commit"],
    "source_manifest": manifest["source_manifest"],
    "runtime_image_id": manifest["build"]["runtime_image_id"],
    "tests": manifest["gates"]["unittest"]["tests"],
}, sort_keys=True, separators=(",", ":")))
PY
}

rollback_release() {
  [[ $# -ge 1 && $# -le 2 ]] || die "rollback expects a release id and optional --restore-db"
  local release_id=$1
  local restore_db=false
  [[ ${2:-} == --restore-db ]] && restore_db=true
  valid_release_id "$release_id" || die "invalid release id"
  local release_dir="${RELEASE_ROOT}/${release_id}"
  local source_dir="${release_dir}/source"
  local evidence_dir="${release_dir}/evidence"
  local rollback_dir="${release_dir}/rollback"
  local previous_json="$evidence_dir/previous.json"
  local manifest="$evidence_dir/release-manifest.json"
  local backup_json="$evidence_dir/backup.json"
  [[ -f "$previous_json" && -f "$manifest" && -f "$backup_json" ]] || die "release rollback metadata is incomplete"
  [[ "$(cat "$APP_ROOT/.release-id")" == "$release_id" ]] || die "requested release is not current"

  local current_commit current_manifest previous_commit previous_manifest previous_image previous_source previous_id compose_kind
  current_commit=$(json_value "$manifest" git_commit)
  current_manifest=$(json_value "$manifest" source_manifest)
  previous_commit=$(json_value "$previous_json" app_commit)
  previous_manifest=$(json_value "$previous_json" source_manifest)
  previous_image=$(json_value "$previous_json" image_id)
  previous_source=$(json_value "$previous_json" source_root)
  previous_id=$(json_value "$previous_json" release_id)
  compose_kind=$(json_value "$previous_json" compose_kind)
  valid_commit "$current_commit" || die "invalid current rollback commit"
  valid_commit "$previous_commit" || die "invalid previous rollback commit"
  valid_sha256 "$current_manifest" || die "invalid current rollback source manifest"
  valid_sha256 "$previous_manifest" || die "invalid previous rollback source manifest"
  [[ "$previous_image" =~ ^sha256:[0-9a-f]{64}$ ]] || die "invalid previous rollback image"
  [[ "$previous_source" == "$APP_ROOT" || "$previous_source" =~ ^${RELEASE_ROOT}/r2-[0-9]{2}([a-z][0-9]*)?-[0-9a-f]{7}-[0-9]{8}T[0-9]{6}Z/source$ ]] || die "unsafe previous source path"
  [[ -d "$previous_source" ]] || die "previous source path is missing"
  [[ "$(python3 "$source_dir/scripts/release_guard.py" source-manifest "$previous_source")" == "$previous_manifest" ]] || die "previous source manifest mismatch"
  docker image inspect "$previous_image" >/dev/null

  local database_backup="$rollback_dir/state-pre.sqlite3"
  local source_backup="$rollback_dir/source-pre.tar.gz"
  local environment_backup="$rollback_dir/env-pre.bak"
  local expected_database_sha expected_source_sha rollback_image_tag
  [[ -f "$database_backup" && -f "$source_backup" && -f "$environment_backup" ]] || die "rollback assets are incomplete"
  expected_database_sha=$(json_value "$backup_json" database.sha256)
  expected_source_sha=$(json_value "$backup_json" source.sha256)
  rollback_image_tag=$(json_value "$backup_json" rollback_image_tag)
  valid_sha256 "$expected_database_sha" || die "invalid database backup hash"
  valid_sha256 "$expected_source_sha" || die "invalid source backup hash"
  [[ "$(sha256sum "$database_backup" | cut -d' ' -f1)" == "$expected_database_sha" ]] || die "database backup hash mismatch"
  [[ "$(sha256sum "$source_backup" | cut -d' ' -f1)" == "$expected_source_sha" ]] || die "source backup hash mismatch"
  [[ "$(stat -c '%a' "$environment_backup")" == 600 ]] || die "environment backup permissions are unsafe"
  python3 "$source_dir/scripts/release_guard.py" db-report "$database_backup" --require-safe >/dev/null
  python3 "$source_dir/scripts/release_guard.py" verify-archive "$source_backup" >/dev/null
  [[ "$(docker image inspect --format '{{.Id}}' "$rollback_image_tag")" == "$previous_image" ]] || die "rollback image tag mismatch"

  local before_version after_version
  before_version=$(json_value "$manifest" database.user_version_before)
  after_version=$(json_value "$manifest" database.user_version_after)
  if [[ "$before_version" != "$after_version" && "$restore_db" != true ]]; then
    die "schema-changing rollback requires --restore-db"
  fi

  stage=rollback-preflight
  local stamp rollback_preflight
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  rollback_preflight="$evidence_dir/rollback-preflight-${stamp}.json"
  python3 "$source_dir/scripts/remote_preflight.py" \
    --require-safe --expect-app-commit "$current_commit" \
    --expect-source-manifest "$current_manifest" >"$rollback_preflight"
  python3 "$source_dir/scripts/release_guard.py" sqlite-backup \
    "$APP_ROOT/data/state.sqlite3" "$rollback_dir/state-before-rollback-${stamp}.sqlite3" \
    >"$evidence_dir/database-before-rollback-${stamp}.json"

  stage=rollback-cutover
  local target_tag="tgvio-rollback-target:${release_id}"
  docker image tag "$previous_image" "$target_tag"
  if [[ "$restore_db" == true ]]; then
    docker compose --project-name "$PROJECT" --env-file "$source_dir/.release.env" \
      -f "$source_dir/docker-compose.yml" stop "$SERVICE"
    python3 "$source_dir/scripts/release_guard.py" sqlite-restore \
      "$rollback_dir/state-pre.sqlite3" "$APP_ROOT/data/state.sqlite3" \
      >"$evidence_dir/database-restored-${stamp}.json"
  fi
  if [[ "$compose_kind" == versioned ]]; then
    local rollback_env="$rollback_dir/rollback-${stamp}.env"
    write_release_env "$rollback_env" "$target_tag" "$previous_commit" "$previous_id" \
      "$previous_manifest" "$(sha256sum "$previous_source/requirements.lock" | cut -d' ' -f1)"
    docker compose --project-name "$PROJECT" --env-file "$rollback_env" \
      -f "$previous_source/docker-compose.yml" up -d --no-build --pull never \
      --force-recreate --no-deps "$SERVICE"
  elif [[ "$compose_kind" == legacy && "$previous_source" == "$APP_ROOT" ]]; then
    docker image tag "$previous_image" tgvio-tgvio:latest
    docker compose --project-name "$PROJECT" -f "$APP_ROOT/docker-compose.yml" \
      up -d --no-build --pull never --force-recreate --no-deps "$SERVICE"
  else
    die "unsupported previous Compose layout"
  fi
  wait_for_health
  atomic_metadata "$previous_source" "$previous_commit" "$previous_id"

  stage=rollback-postflight
  local rollback_postflight="$evidence_dir/rollback-postflight-${stamp}.json"
  python3 "$source_dir/scripts/remote_preflight.py" \
    --source-root "$previous_source" --expect-app-commit "$previous_commit" \
    --expect-source-manifest "$previous_manifest" --expect-image "$previous_image" \
    >"$rollback_postflight"
  assert_postflight "$rollback_postflight"
  printf 'rollback_complete release=%s restored=%s\n' "$release_id" "$previous_id"
}

rollback_check() {
  [[ $# -eq 1 ]] || die "rollback-check expects one release id"
  local release_id=$1
  valid_release_id "$release_id" || die "invalid release id"
  local release_dir="${RELEASE_ROOT}/${release_id}"
  local previous_json="$release_dir/evidence/previous.json"
  local manifest="$release_dir/evidence/release-manifest.json"
  local backup_json="$release_dir/evidence/backup.json"
  local rollback_dir="$release_dir/rollback"
  [[ -f "$previous_json" && -f "$manifest" && -f "$backup_json" ]] || die "release rollback metadata is incomplete"
  [[ "$(cat "$APP_ROOT/.release-id")" == "$release_id" ]] || die "requested release is not current"
  local previous_source previous_image previous_manifest
  previous_source=$(json_value "$previous_json" source_root)
  previous_image=$(json_value "$previous_json" image_id)
  previous_manifest=$(json_value "$previous_json" source_manifest)
  [[ "$previous_source" == "$APP_ROOT" || "$previous_source" =~ ^${RELEASE_ROOT}/r2-[0-9]{2}([a-z][0-9]*)?-[0-9a-f]{7}-[0-9]{8}T[0-9]{6}Z/source$ ]] || die "unsafe previous source path"
  [[ -d "$previous_source" ]] || die "previous source is missing"
  valid_sha256 "$previous_manifest" || die "invalid previous source manifest"
  [[ "$(python3 "$release_dir/source/scripts/release_guard.py" source-manifest "$previous_source")" == "$previous_manifest" ]] || die "previous source manifest mismatch"
  docker image inspect "$previous_image" >/dev/null

  local database_backup="$rollback_dir/state-pre.sqlite3"
  local source_backup="$rollback_dir/source-pre.tar.gz"
  local environment_backup="$rollback_dir/env-pre.bak"
  local expected_database_sha expected_source_sha rollback_image_tag
  [[ -f "$database_backup" && -f "$source_backup" && -f "$environment_backup" ]] || die "rollback assets are incomplete"
  expected_database_sha=$(json_value "$backup_json" database.sha256)
  expected_source_sha=$(json_value "$backup_json" source.sha256)
  rollback_image_tag=$(json_value "$backup_json" rollback_image_tag)
  valid_sha256 "$expected_database_sha" || die "invalid database backup hash"
  valid_sha256 "$expected_source_sha" || die "invalid source backup hash"
  [[ "$(sha256sum "$database_backup" | cut -d' ' -f1)" == "$expected_database_sha" ]] || die "database backup hash mismatch"
  [[ "$(sha256sum "$source_backup" | cut -d' ' -f1)" == "$expected_source_sha" ]] || die "source backup hash mismatch"
  [[ "$(stat -c '%a' "$environment_backup")" == 600 ]] || die "environment backup permissions are unsafe"
  python3 "$release_dir/source/scripts/release_guard.py" db-report "$database_backup" --require-safe >/dev/null
  python3 "$release_dir/source/scripts/release_guard.py" verify-archive "$source_backup" >/dev/null
  [[ "$(docker image inspect --format '{{.Id}}' "$rollback_image_tag")" == "$previous_image" ]] || die "rollback image tag mismatch"
  printf 'rollback_check=passed release=%s\n' "$release_id"
}

for command in docker python3 realpath sha256sum stat tar; do
  require_command "$command"
done
[[ $(id -u) -eq 0 ]] || die "remote release must run as root"

case ${1:-} in
  deploy)
    shift
    deploy_release "$@"
    ;;
  rollback)
    shift
    rollback_release "$@"
    ;;
  rollback-check)
    shift
    rollback_check "$@"
    ;;
  *)
    die "usage: remote_release.sh deploy ... | rollback RELEASE_ID [--restore-db] | rollback-check RELEASE_ID"
    ;;
esac
