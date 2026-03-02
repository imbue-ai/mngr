help:
    @just --list

build target:
  @if [ "{{target}}" = "flexmux" ]; then \
    cd libs/flexmux/frontend && pnpm install && pnpm run build; \
  elif [ "{{target}}" = "claude_web_view" ]; then \
    cd apps/claude_web_view/frontend && pnpm install && pnpm run build; \
  elif [ -d "apps/{{target}}" ]; then \
    uvx --from build pyproject-build --installer=uv --outdir=dist --wheel apps/{{target}}; \
  elif [ -d "libs/{{target}}" ]; then \
    uvx --from build pyproject-build --installer=uv --outdir=dist --wheel libs/{{target}}; \
  else \
    echo "Error: Target '{{target}}' not found in apps/ or libs/"; \
    exit 1; \
  fi

run target:
  @if [ "{{target}}" = "flexmux" ]; then \
    uv run flexmux; \
  else \
    echo "Error: No run command defined for '{{target}}'"; \
    exit 1; \
  fi

# Run tests on Modal via Offload 
test-offload args="":
    #!/bin/bash
    set -ueo pipefail
    # If not set, default LAST_COMMIT_SHA to the current HEAD
    export LAST_COMMIT_SHA=${LAST_COMMIT_SHA:-$(git rev-parse HEAD)}
    tmpdir=$(mktemp -d)
    trap "rm -rf $tmpdir" EXIT

    ./scripts/make_tar_of_repo.sh $LAST_COMMIT_SHA $tmpdir
    export OFFLOAD_PATCH_UUID=`uv run python -c"import uuid;print(uuid.uuid4())"`
    mkdir -p /tmp/$OFFLOAD_PATCH_UUID
    trap "rm -rf /tmp/$OFFLOAD_PATCH_UUID; rm -rf $tmpdir" EXIT

    ./scripts/generate_patch_for_offload.sh $LAST_COMMIT_SHA > /tmp/$OFFLOAD_PATCH_UUID/patch
    cp $tmpdir/current.tar.gz .
    trap "rm -f current.tar.gz; rm -rf /tmp/$OFFLOAD_PATCH_UUID; rm -rf $tmpdir" EXIT

    # Run offload, and make sure to specifically permit error code 2 (flaky tests). Any other error code is a failure.
    offload -c offload-modal.toml {{args}} run --env="LAST_COMMIT_SHA=${LAST_COMMIT_SHA}" --copy-dir="/tmp/$OFFLOAD_PATCH_UUID:/offload-upload" || [[ $? -eq 2 ]]

# Run acceptance tests on Modal via Offload
test-offload-acceptance args="":
    #!/bin/bash
    set -ueo pipefail
    # If not set, default LAST_COMMIT_SHA to the current HEAD
    export LAST_COMMIT_SHA=${LAST_COMMIT_SHA:-$(git rev-parse HEAD)}
    tmpdir=$(mktemp -d)
    trap "rm -rf $tmpdir" EXIT

    ./scripts/make_tar_of_repo.sh $LAST_COMMIT_SHA $tmpdir
    export OFFLOAD_PATCH_UUID=`uv run python -c"import uuid;print(uuid.uuid4())"`
    mkdir -p /tmp/$OFFLOAD_PATCH_UUID
    trap "rm -rf /tmp/$OFFLOAD_PATCH_UUID; rm -rf $tmpdir" EXIT

    ./scripts/generate_patch_for_offload.sh $LAST_COMMIT_SHA > /tmp/$OFFLOAD_PATCH_UUID/patch
    cp $tmpdir/current.tar.gz .
    trap "rm -f current.tar.gz; rm -rf /tmp/$OFFLOAD_PATCH_UUID; rm -rf $tmpdir" EXIT

    # Run offload, and make sure to specifically permit error code 2 (flaky tests). Any other error code is a failure.
    offload -c offload-modal-acceptance.toml {{args}} run --env="LAST_COMMIT_SHA=${LAST_COMMIT_SHA}" --copy-dir="/tmp/$OFFLOAD_PATCH_UUID:/offload-upload" --env "MODAL_TOKEN_ID=$MODAL_TOKEN_ID" --env "MODAL_TOKEN_SECRET=$MODAL_TOKEN_SECRET" || [[ $? -eq 2 ]]

test-unit:
  uv run pytest --ignore-glob="**/test_*.py" --cov-fail-under=36

test-integration:
  uv run pytest

# can run without coverage to make things slightly faster when checking locally
test-quick:
  uv run pytest --no-cov --cov-fail-under=0

test-acceptance:
  # when running these locally, we set the max duration super high just so that we don't fail (which makes it harder to see the errors)
  # parallelism is controlled by PYTEST_NUMPROCESSES env var (default: 4 from pyproject.toml)
  PYTEST_MAX_DURATION=600 uv run pytest --override-ini='cov-fail-under=0' --no-cov -m "no release"

test-release:
  # when running these locally, we set the max duration super high just so that we don't fail (which makes it harder to see the errors)
  # parallelism is controlled by PYTEST_NUMPROCESSES env var (default: 4 from pyproject.toml)
  PYTEST_MAX_DURATION=1200 uv run pytest --override-ini='cov-fail-under=0' --no-cov -m "acceptance or not acceptance"

# Generate test timings for pytest-split (run periodically to keep timings up to date. Runs all acceptance and release)
test-timings:
  # when running these locally, we set the max duration super high just so that we don't fail (which makes it harder to see the errors)
  PYTEST_MAX_DURATION=6000 uv run pytest --override-ini='cov-fail-under=0' --no-cov -n 0 -m "acceptance or not acceptance" --store-durations

# useful for running against a single test, regardless of how it is marked
test target:
  PYTEST_MAX_DURATION=600 uv run pytest -sv --override-ini='cov-fail-under=0' --no-cov -n 0 -m "acceptance or not acceptance" "{{target}}"
