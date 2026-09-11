#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"

for command in git docker python3 sha256sum; do
  command -v "$command" >/dev/null 2>&1 || {
    printf 'missing command: %s\n' "$command" >&2
    exit 2
  }
done

python_guard=(python3 scripts/release_guard.py)
"${python_guard[@]}" verify-tree "$repo_root"
"${python_guard[@]}" architecture "$repo_root"
"${python_guard[@]}" repository "$repo_root" --require-clean --require-pushed
git diff --check HEAD^ HEAD

commit=$(git rev-parse HEAD)
source_manifest=$("${python_guard[@]}" source-manifest "$repo_root")
lock_sha=$(sha256sum requirements.lock | cut -d' ' -f1)
short_commit=${commit:0:12}
test_tag="tgvio-check-test:${short_commit}"
runtime_tag="tgvio-check-runtime:${short_commit}"

docker compose --env-file deploy/compose.placeholder.env -f docker-compose.yml config --quiet

build_args=(
  --build-arg "APP_COMMIT=${commit}"
  --build-arg "RELEASE_ID=check-${short_commit}"
  --build-arg "SOURCE_MANIFEST=${source_manifest}"
  --build-arg "REQUIREMENTS_LOCK_SHA256=${lock_sha}"
)

DOCKER_BUILDKIT=1 docker build --pull=false --target test --tag "$test_tag" \
  "${build_args[@]}" .
docker run --rm --network none "$test_tag"

DOCKER_BUILDKIT=1 docker build --pull=false --target runtime --tag "$runtime_tag" \
  "${build_args[@]}" .
docker run --rm --network none --entrypoint python "$runtime_tag" \
  scripts/image_inspect.py --expect-source-manifest "$source_manifest"

printf 'check_build=passed runtime_image=%s commit=%s\n' \
  "$(docker image inspect --format '{{.Id}}' "$runtime_tag")" "$commit"
printf 'classification=test-build; this command never deploys or starts the Bot\n'
