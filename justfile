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
    # Run offload with checkpoint-based image caching (permit exit code 2 = flaky tests).
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
    # Run offload with checkpoint-based image caching (permit exit code 2 = flaky tests).
    offload -c offload-modal-acceptance.toml {{args}} run --env "MODAL_TOKEN_ID=$MODAL_TOKEN_ID" --env "MODAL_TOKEN_SECRET=$MODAL_TOKEN_SECRET" || [[ $? -eq 2 ]]

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
