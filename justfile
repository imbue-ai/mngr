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

    # Generate .dockerignore from .gitignore: remove the current.tar.gz line
    # (needed in the Docker build context) and add .git/ (not in .gitignore).
    grep -v 'current\.tar\.gz' .gitignore > .dockerignore
    echo '.git/' >> .dockerignore

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

    # Generate .dockerignore from .gitignore: remove the current.tar.gz line
    # (needed in the Docker build context) and add .git/ (not in .gitignore).
    grep -v 'current\.tar\.gz' .gitignore > .dockerignore
    echo '.git/' >> .dockerignore

    ./scripts/make_tar_of_repo.sh $BASE_COMMIT $tmpdir
    export OFFLOAD_PATCH_UUID=`uv run python -c"import uuid;print(uuid.uuid4())"`
    mkdir -p /tmp/$OFFLOAD_PATCH_UUID
    trap "rm -f .dockerignore; rm -rf /tmp/$OFFLOAD_PATCH_UUID; rm -rf $tmpdir" EXIT

    ./scripts/generate_patch_for_offload.sh $BASE_COMMIT > /tmp/$OFFLOAD_PATCH_UUID/patch
    cp $tmpdir/current.tar.gz .
    trap "rm -f current.tar.gz .dockerignore; rm -rf /tmp/$OFFLOAD_PATCH_UUID; rm -rf $tmpdir" EXIT

    # Run offload, and make sure to specifically permit error code 2 (flaky tests). Any other error code is a failure.
    offload -c offload-modal-acceptance.toml {{args}} run --copy-dir="/tmp/$OFFLOAD_PATCH_UUID:/offload-upload" --env "MODAL_TOKEN_ID=$MODAL_TOKEN_ID" --env "MODAL_TOKEN_SECRET=$MODAL_TOKEN_SECRET" || [[ $? -eq 2 ]]

# Xdist parallelism args for local dev recipes. Kept out of pyproject addopts
# so they don't leak into offload sandboxes (which run `-p no:xdist`).
_parallel := "-n 4 --dist=worksteal --max-worker-restart=0"
# Default mark filter for local unit + integration recipes. Kept out of
# pyproject addopts because it would collide with offload-modal-acceptance
# (which runs the opposite filter). A later -m on CLI overrides this.
_skip_acceptance_and_release := "-m 'not acceptance and not release'"

test-unit:
  uv run pytest {{_parallel}} {{_skip_acceptance_and_release}} --cov-report=html --ignore-glob="**/test_*.py" --cov-fail-under=36

test-integration:
  uv run pytest {{_parallel}} {{_skip_acceptance_and_release}} --cov-report=html --cov-fail-under=80

# Fast local iteration: forwards args to pytest. No coverage, xdist-parallel.
# Examples:
#   just test-quick
#   just test-quick libs/mngr
#   just test-quick libs/mngr/.../foo_test.py::test_bar
#   just test-quick "libs/mngr -m 'not tmux and not modal'"
# Note: pass complex argument strings (anything with spaces, like -m exprs)
# as ONE outer-quoted argument. Variadic {{args}} splits on whitespace
# and drops inner quoting, which would truncate `-m 'a and b'` to `-m a`.
# The recipe's default `-m 'not acceptance and not release'` can be
# overridden by supplying a `-m` inside args (later CLI -m wins).
test-quick args="":
  uv run pytest {{_parallel}} {{_skip_acceptance_and_release}} --no-cov {{args}}

test-acceptance:
  # when running these locally, we set the max duration super high just so that we don't fail (which makes it harder to see the errors)
  PYTEST_MAX_DURATION_SECONDS=600 uv run pytest {{_parallel}} --no-cov -m "no release"

test-release:
  # when running these locally, we set the max duration super high just so that we don't fail (which makes it harder to see the errors)
  PYTEST_MAX_DURATION_SECONDS=1200 uv run pytest {{_parallel}} --no-cov -m "acceptance or not acceptance"

# Generate test timings for pytest-split (run periodically to keep timings up to date. Runs all acceptance and release)
test-timings:
  # when running these locally, we set the max duration super high just so that we don't fail (which makes it harder to see the errors)
  PYTEST_MAX_DURATION_SECONDS=6000 uv run pytest --no-cov -n 0 -m "acceptance or not acceptance" --store-durations

# useful for running against a single test, regardless of how it is marked
test target:
  PYTEST_MAX_DURATION_SECONDS=600 uv run pytest -sv --no-cov -n 0 -m "acceptance or not acceptance" "{{target}}"
