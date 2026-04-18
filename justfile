help:
    @just --list

build target:
  @if [ "{{target}}" = "flexmux" ]; then \
    cd libs/flexmux/frontend && pnpm install && pnpm run build; \
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

# Generate .dockerignore from .gitignore: remove the current.tar.gz line
# (needed in the Docker build context) and add tracked files that offload
# modifies during builds (which causes Modal upload errors).
[private]
_generate-dockerignore:
    # Strip current.tar.gz (needed in docker build context) and /.dockerignore
    # (would cause .dockerignore to ignore itself when .gitignore lists it).
    grep -vE 'current\.tar\.gz|^/?\.dockerignore$' .gitignore > .dockerignore
    echo '.git/' >> .dockerignore
    echo '.offload-image-cache' >> .dockerignore
    # Glob covers .offload-cache-key and every per-target .offload-<target>-cache-key
    # file so adding a new test-offload-<target> recipe doesn't require editing here.
    echo '.offload-*cache-key' >> .dockerignore

# Generate Dockerfile.release from Dockerfile + Dockerfile.release.extras so the
# two Dockerfiles stay in sync without duplicating the base layers. Strips the
# trailing CMD from Dockerfile; the .extras file provides its own CMD.
[private]
_generate-release-dockerfile:
    #!/bin/bash
    set -ueo pipefail
    base=libs/mngr/imbue/mngr/resources/Dockerfile
    extras=libs/mngr/imbue/mngr/resources/Dockerfile.release.extras
    out=libs/mngr/imbue/mngr/resources/Dockerfile.release
    # Everything from the base Dockerfile up to (not including) the first CMD line.
    awk '/^CMD/{exit} {print}' "$base" > "$out"
    cat "$extras" >> "$out"

# Run tests on Modal via Offload
test-offload args="":
    #!/bin/bash
    set -ueo pipefail
    BASE_COMMIT=$(cat .offload-base-commit | tr -d '[:space:]')
    tmpdir=$(mktemp -d)
    trap "rm -rf $tmpdir" EXIT

    # Invalidate offload's image cache when build inputs change.
    # Offload only caches by image ID and doesn't track Dockerfile or base commit changes.
    CACHE_KEY=$(cat .offload-base-commit libs/mngr/imbue/mngr/resources/Dockerfile offload-modal.toml | shasum -a 256 | cut -d' ' -f1)
    CACHE_KEY_FILE=".offload-cache-key"
    if [ -f "$CACHE_KEY_FILE" ] && [ "$(cat "$CACHE_KEY_FILE")" = "$CACHE_KEY" ]; then
        echo "[test-offload] Image cache key matches, reusing cached image."
    else
        echo "[test-offload] Image cache key changed, clearing cached image."
        rm -f .offload-image-cache
        echo "$CACHE_KEY" > "$CACHE_KEY_FILE"
    fi

    just _generate-dockerignore

    ./scripts/make_tar_of_repo.sh $BASE_COMMIT $tmpdir
    export OFFLOAD_PATCH_UUID=`uv run python -c"import uuid;print(uuid.uuid4())"`
    mkdir -p /tmp/$OFFLOAD_PATCH_UUID
    trap "rm -f .dockerignore; rm -rf /tmp/$OFFLOAD_PATCH_UUID; rm -rf $tmpdir" EXIT

    ./scripts/generate_patch_for_offload.sh $BASE_COMMIT > /tmp/$OFFLOAD_PATCH_UUID/patch
    cp $tmpdir/current.tar.gz .
    trap "rm -f current.tar.gz .dockerignore; rm -rf /tmp/$OFFLOAD_PATCH_UUID; rm -rf $tmpdir" EXIT

    # Run offload, and make sure to specifically permit error code 2 (flaky tests). Any other error code is a failure.
    offload -c offload-modal.toml {{args}} run --copy-dir="/tmp/$OFFLOAD_PATCH_UUID:/offload-upload" || [[ $? -eq 2 ]]

    # Copy results to the main worktree so new worktrees inherit baselines via COPY mode.
    MAIN_WORKTREE=$(git worktree list --porcelain | head -1 | sed 's/^worktree //')
    if [ -f test-results/junit.xml ] && [ -n "$MAIN_WORKTREE" ] && [ "$MAIN_WORKTREE" != "$(pwd)" ]; then
        mkdir -p "$MAIN_WORKTREE/test-results"
        cp test-results/junit.xml "$MAIN_WORKTREE/test-results/junit.xml"
    fi

# Run acceptance tests on Modal via Offload
test-offload-acceptance args="":
    #!/bin/bash
    set -ueo pipefail
    BASE_COMMIT=$(cat .offload-base-commit | tr -d '[:space:]')
    tmpdir=$(mktemp -d)
    trap "rm -rf $tmpdir" EXIT

    # Invalidate offload's image cache when build inputs change. The
    # acceptance config uses libs/mngr/imbue/mngr/resources/Dockerfile
    # (the Docker startup scripts are only inputs for the release
    # recipe, which uses Dockerfile.release and COPYs them in).
    CACHE_KEY=$(cat .offload-base-commit \
        libs/mngr/imbue/mngr/resources/Dockerfile \
        offload-modal-acceptance.toml | shasum -a 256 | cut -d' ' -f1)
    CACHE_KEY_FILE=".offload-acceptance-cache-key"
    if [ -f "$CACHE_KEY_FILE" ] && [ "$(cat "$CACHE_KEY_FILE")" = "$CACHE_KEY" ]; then
        echo "[test-offload-acceptance] Image cache key matches, reusing cached image."
    else
        echo "[test-offload-acceptance] Image cache key changed, clearing cached image."
        rm -f .offload-image-cache
        echo "$CACHE_KEY" > "$CACHE_KEY_FILE"
    fi

    just _generate-dockerignore

    ./scripts/make_tar_of_repo.sh $BASE_COMMIT $tmpdir
    export OFFLOAD_PATCH_UUID=`uv run python -c"import uuid;print(uuid.uuid4())"`
    mkdir -p /tmp/$OFFLOAD_PATCH_UUID
    trap "rm -f .dockerignore; rm -rf /tmp/$OFFLOAD_PATCH_UUID; rm -rf $tmpdir" EXIT

    ./scripts/generate_patch_for_offload.sh $BASE_COMMIT > /tmp/$OFFLOAD_PATCH_UUID/patch
    cp $tmpdir/current.tar.gz .
    trap "rm -f current.tar.gz .dockerignore; rm -rf /tmp/$OFFLOAD_PATCH_UUID; rm -rf $tmpdir" EXIT

    # Run offload, and make sure to specifically permit error code 2 (flaky tests). Any other error code is a failure.
    # MODAL_IMAGE_BUILDER_VERSION=2025.06 is required for enable_docker support (Docker-in-Docker alpha).
    MODAL_IMAGE_BUILDER_VERSION=2025.06 offload -c offload-modal-acceptance.toml {{args}} run --copy-dir="/tmp/$OFFLOAD_PATCH_UUID:/offload-upload" --env "MODAL_TOKEN_ID=$MODAL_TOKEN_ID" --env "MODAL_TOKEN_SECRET=$MODAL_TOKEN_SECRET" || [[ $? -eq 2 ]]

# Run release tests on Modal via Offload (with Docker-in-Docker)
test-offload-release args="":
    #!/bin/bash
    set -ueo pipefail
    BASE_COMMIT=$(cat .offload-base-commit | tr -d '[:space:]')
    tmpdir=$(mktemp -d)
    trap "rm -rf $tmpdir" EXIT

    # Regenerate Dockerfile.release from Dockerfile + Dockerfile.release.extras
    # so the two stay in sync.
    just _generate-release-dockerfile

    # Invalidate offload's image cache when build inputs change.
    # Include the Docker startup scripts COPY'd into the image so that edits
    # to those scripts also invalidate the cache.
    CACHE_KEY=$(cat .offload-base-commit \
        libs/mngr/imbue/mngr/resources/Dockerfile \
        libs/mngr/imbue/mngr/resources/Dockerfile.release.extras \
        libs/mngr/imbue/mngr/resources/start-dockerd.sh \
        libs/mngr/imbue/mngr/resources/ensure-dockerd.sh \
        offload-modal-release.toml | shasum -a 256 | cut -d' ' -f1)
    CACHE_KEY_FILE=".offload-release-cache-key"
    if [ -f "$CACHE_KEY_FILE" ] && [ "$(cat "$CACHE_KEY_FILE")" = "$CACHE_KEY" ]; then
        echo "[test-offload-release] Image cache key matches, reusing cached image."
    else
        echo "[test-offload-release] Image cache key changed, clearing cached image."
        rm -f .offload-image-cache
        echo "$CACHE_KEY" > "$CACHE_KEY_FILE"
    fi

    just _generate-dockerignore

    ./scripts/make_tar_of_repo.sh $BASE_COMMIT $tmpdir
    export OFFLOAD_PATCH_UUID=`uv run python -c"import uuid;print(uuid.uuid4())"`
    mkdir -p /tmp/$OFFLOAD_PATCH_UUID
    trap "rm -f .dockerignore; rm -rf /tmp/$OFFLOAD_PATCH_UUID; rm -rf $tmpdir" EXIT

    ./scripts/generate_patch_for_offload.sh $BASE_COMMIT > /tmp/$OFFLOAD_PATCH_UUID/patch
    cp $tmpdir/current.tar.gz .
    trap "rm -f current.tar.gz .dockerignore; rm -rf /tmp/$OFFLOAD_PATCH_UUID; rm -rf $tmpdir" EXIT

    # Run offload, and make sure to specifically permit error code 2 (flaky tests). Any other error code is a failure.
    # MODAL_IMAGE_BUILDER_VERSION=2025.06 is required for enable_docker support (Docker-in-Docker alpha).
    MODAL_IMAGE_BUILDER_VERSION=2025.06 offload -c offload-modal-release.toml {{args}} run --copy-dir="/tmp/$OFFLOAD_PATCH_UUID:/offload-upload" --env "MODAL_TOKEN_ID=$MODAL_TOKEN_ID" --env "MODAL_TOKEN_SECRET=$MODAL_TOKEN_SECRET" --env "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" --env "IS_RELEASE=1" || [[ $? -eq 2 ]]

    # Copy results to the main worktree so new worktrees inherit baselines via COPY mode.
    MAIN_WORKTREE=$(git worktree list --porcelain | head -1 | sed 's/^worktree //')
    if [ -f test-results/junit.xml ] && [ -n "$MAIN_WORKTREE" ] && [ "$MAIN_WORKTREE" != "$(pwd)" ]; then
        mkdir -p "$MAIN_WORKTREE/test-results"
        cp test-results/junit.xml "$MAIN_WORKTREE/test-results/junit.xml"
    fi

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
  PYTEST_MAX_DURATION_SECONDS=600 uv run pytest --override-ini='cov-fail-under=0' --no-cov -m "no release"

test-release:
  # when running these locally, we set the max duration super high just so that we don't fail (which makes it harder to see the errors)
  # parallelism is controlled by PYTEST_NUMPROCESSES env var (default: 4 from pyproject.toml)
  PYTEST_MAX_DURATION_SECONDS=1200 uv run pytest --override-ini='cov-fail-under=0' --no-cov -m "acceptance or not acceptance"

# Generate test timings for pytest-split (run periodically to keep timings up to date. Runs all acceptance and release)
test-timings:
  # when running these locally, we set the max duration super high just so that we don't fail (which makes it harder to see the errors)
  PYTEST_MAX_DURATION_SECONDS=6000 uv run pytest --override-ini='cov-fail-under=0' --no-cov -n 0 -m "acceptance or not acceptance" --store-durations

# useful for running against a single test, regardless of how it is marked
test target:
  PYTEST_MAX_DURATION_SECONDS=600 uv run pytest -sv --override-ini='cov-fail-under=0' --no-cov -n 0 -m "acceptance or not acceptance" "{{target}}"
