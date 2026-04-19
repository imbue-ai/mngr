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

# Generate .dockerignore from .gitignore. Kept as a generator (not a
# committed file) so .dockerignore stays in lockstep with .gitignore and
# never needs manual sync. The generator strips the `/.dockerignore` self-
# reference so offload 0.7.1's copy_untracked_files picks up this
# gitignored-but-not-dockerignored file and ships it into the exported
# build context, where Docker uses it to filter what lands in the image.
[private]
_generate-dockerignore:
    grep -vE '^/?\.dockerignore$' .gitignore > .dockerignore

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
    : "${MODAL_TOKEN_ID:?must be set}"
    : "${MODAL_TOKEN_SECRET:?must be set}"
    just _generate-dockerignore
    trap "rm -f .dockerignore" EXIT
    # offload 0.7.1 manages build context + image caching via git notes; no
    # tarballs, patches, or local cache-key files needed.
    offload -c offload-modal.toml {{args}} run || [[ $? -eq 2 ]]

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
    : "${MODAL_TOKEN_ID:?must be set}"
    : "${MODAL_TOKEN_SECRET:?must be set}"
    just _generate-dockerignore
    trap "rm -f .dockerignore" EXIT
    # MODAL_IMAGE_BUILDER_VERSION=2025.06 is required for enable_docker support (Docker-in-Docker alpha).
    MODAL_IMAGE_BUILDER_VERSION=2025.06 offload -c offload-modal-acceptance.toml {{args}} run \
        --env "MODAL_TOKEN_ID=$MODAL_TOKEN_ID" \
        --env "MODAL_TOKEN_SECRET=$MODAL_TOKEN_SECRET" || [[ $? -eq 2 ]]

# Run release tests on Modal via Offload (with Docker-in-Docker)
test-offload-release args="":
    #!/bin/bash
    set -ueo pipefail
    : "${MODAL_TOKEN_ID:?must be set}"
    : "${MODAL_TOKEN_SECRET:?must be set}"
    : "${ANTHROPIC_API_KEY:?must be set}"

    # Regenerate Dockerfile.release from Dockerfile + Dockerfile.release.extras
    # so the two stay in sync.
    just _generate-release-dockerfile
    just _generate-dockerignore
    trap "rm -f .dockerignore" EXIT

    # MODAL_IMAGE_BUILDER_VERSION=2025.06 is required for enable_docker support (Docker-in-Docker alpha).
    # GITHUB_TOKEN authorises cloning the private forever-claude-template repo
    # used by minds desktop-client e2e tests; passing it unset is fine and makes
    # the dev opt-in (tests that don't need the clone still run).
    MODAL_IMAGE_BUILDER_VERSION=2025.06 offload -c offload-modal-release.toml {{args}} run \
        --env "MODAL_TOKEN_ID=$MODAL_TOKEN_ID" \
        --env "MODAL_TOKEN_SECRET=$MODAL_TOKEN_SECRET" \
        --env "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" \
        --env "GITHUB_TOKEN=${GITHUB_TOKEN:-}" \
        --env "IS_RELEASE=1" || [[ $? -eq 2 ]]

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
