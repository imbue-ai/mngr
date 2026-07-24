help:
    @just --list

build target:
  @if [ -d "apps/{{target}}" ]; then \
    uvx --from build pyproject-build --installer=uv --outdir=dist --wheel apps/{{target}}; \
  elif [ -d "libs/{{target}}" ]; then \
    uvx --from build pyproject-build --installer=uv --outdir=dist --wheel libs/{{target}}; \
  else \
    echo "Error: Target '{{target}}' not found in apps/ or libs/"; \
    exit 1; \
  fi

# Xdist parallelism args for local dev recipes. Kept out of pyproject addopts
# so they don't leak into offload sandboxes (which run `-p no:xdist`).
_parallel := "-n 4 --dist=worksteal --max-worker-restart=0"
# Default mark filter for local unit + integration recipes. Kept out of
# pyproject addopts because it would collide with offload-modal-acceptance
# (which runs the opposite filter). A later -m on CLI overrides this.
_skip_acceptance_and_release := "-m 'not acceptance and not release and not minds_deployment and not minds_services and not minds_snapshot_resume'"

# Coverage report flags are passed explicitly here (not via root addopts) so
# offload CI batches can suppress them -- see the NOTE in root addopts.
# --coverage-to-file keeps the term-missing report out of the terminal and
# writes it to .test_output/ instead.
test-unit:
  uv run pytest {{_parallel}} {{_skip_acceptance_and_release}} --cov-report=term-missing --cov-report=xml --cov-report=html --coverage-to-file --ignore-glob="**/test_*.py" --cov-fail-under=36

test-integration:
  uv run pytest {{_parallel}} {{_skip_acceptance_and_release}} --cov-report=term-missing --cov-report=xml --cov-report=html --coverage-to-file --cov-fail-under=80

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
# Fast local iteration: forwards args to pytest. No coverage, xdist-parallel.
test-quick args="":
  uv run pytest {{_parallel}} {{_skip_acceptance_and_release}} --no-cov {{args}}

# Regenerate the code-derived agent capability matrix doc (libs/mngr/docs/concepts/agent_capabilities.md)
regenerate-agent-capabilities-doc:
  uv run python scripts/make_agent_capabilities_doc.py

test-acceptance:
  # when running these locally, we set the max duration super high just so that we don't fail (which makes it harder to see the errors)
  PYTEST_MAX_DURATION_SECONDS=600 uv run pytest {{_parallel}} --no-cov -m "not release"

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

# Run the opt-in live Claude Agent SDK tests (libs/mngr_robinhood). These make real,
# paid API calls and are excluded from every CI run. ANTHROPIC_API_KEY must already be
# exported (e.g. `set -a; source .env; set +a`). Pass extra pytest args via `args`.
test-sdk-live args="":
  RUN_SDK_LIVE_TESTS=1 PYTEST_MAX_DURATION_SECONDS=2400 uv run pytest -sv --no-cov -n 0 -o timeout=900 -m sdk_live libs/mngr_robinhood {{args}}


# Diffs against the real base branch, so it must run on a real checkout
# (locally or the GitHub Actions runner), NOT inside an offload sandbox -- the
# sandbox has no base ref and the check would pass vacuously. Bare `python`
# (no `uv run`) because the gate is deliberately stdlib-only: no `uv sync`,
# matching how the `check-changelog` CI job invokes it.
# Check that this branch has a changelog entry per project it touches.
check-changelog:
    python -m scripts.check_changelog_entries

# Ops recipes live in private.just, which is absent on the public mirror.
import? 'private.just'
