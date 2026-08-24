help:
    @just --list

[group("mngr build")]
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
[group("mngr test")]
test-unit:
  uv run pytest {{_parallel}} {{_skip_acceptance_and_release}} --cov-report=term-missing --cov-report=xml --cov-report=html --coverage-to-file --ignore-glob="**/test_*.py" --cov-fail-under=36

[group("mngr test")]
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
[group("mngr test")]
test-quick args="":
  uv run pytest {{_parallel}} {{_skip_acceptance_and_release}} --no-cov {{args}}

# Regenerate the code-derived agent capability matrix doc (libs/mngr/docs/concepts/agent_capabilities.md)
[group("mngr dev")]
regenerate-agent-capabilities-doc:
  uv run python scripts/make_agent_capabilities_doc.py

[group("mngr test")]
test-acceptance:
  # when running these locally, we set the max duration super high just so that we don't fail (which makes it harder to see the errors)
  PYTEST_MAX_DURATION_SECONDS=600 uv run pytest {{_parallel}} --no-cov -m "not release"

[group("mngr test")]
test-release:
  # when running these locally, we set the max duration super high just so that we don't fail (which makes it harder to see the errors)
  PYTEST_MAX_DURATION_SECONDS=1200 uv run pytest {{_parallel}} --no-cov -m "acceptance or not acceptance"

# Generate test timings for pytest-split (run periodically to keep timings up to date. Runs all acceptance and release)
[group("mngr test")]
test-timings:
  # when running these locally, we set the max duration super high just so that we don't fail (which makes it harder to see the errors)
  PYTEST_MAX_DURATION_SECONDS=6000 uv run pytest --no-cov -n 0 -m "acceptance or not acceptance" --store-durations

# useful for running against a single test, regardless of how it is marked
[group("mngr test")]
test target:
  PYTEST_MAX_DURATION_SECONDS=600 uv run pytest -sv --no-cov -n 0 -m "acceptance or not acceptance" "{{target}}"

# Run the opt-in live Claude Agent SDK tests (libs/mngr_robinhood). These make real,
# paid API calls and are excluded from every CI run. ANTHROPIC_API_KEY must already be
# exported (e.g. `set -a; source .env; set +a`). Pass extra pytest args via `args`.
[group("mngr test")]
test-sdk-live args="":
  RUN_SDK_LIVE_TESTS=1 PYTEST_MAX_DURATION_SECONDS=2400 uv run pytest -sv --no-cov -n 0 -o timeout=900 -m sdk_live libs/mngr_robinhood {{args}}


# Deploy the apt mirror Worker (apps/apt_mirror/worker) to the production
# Cloudflare account. Requires CLOUDFLARE_API_TOKEN in the environment -- use
# the APT_MIRROR_DEPLOY_CLOUDFLARE_API_TOKEN value from the
# secrets/minds/production/apt-mirror Vault entry (see apps/apt_mirror/README.md).
[group("apt-mirror ops")]
deploy-apt-mirror:
  cd apps/apt_mirror/worker && pnpm install --frozen-lockfile && pnpm exec wrangler deploy

# Run the apt mirror Worker's vitest suite (workerd runtime, mocked upstreams).
[group("apt-mirror test")]
test-apt-mirror-worker:
  cd apps/apt_mirror/worker && pnpm install --frozen-lockfile && pnpm run typecheck && pnpm test

# Run the minds JS suites (Electron shell node:test units + SPA frontend vitest).
[group("minds test")]
test-minds-js:
  # --ignore-scripts: the units load `semver`, read `electron-updater`'s
  # AppUpdater.js off disk, and are otherwise node builtins -- so the packages
  # are needed but their postinstalls are not, and a plain install fetches the
  # Electron binary and Playwright's browsers to run neither.
  cd apps/minds && pnpm install --frozen-lockfile --ignore-scripts && pnpm test:unit
  # `generate` before `check`: src/generated/ is gitignored, so a fresh checkout
  # has no ui.ts and tsc would otherwise fail on the missing import rather than
  # on anything real. `check` at all because vitest transpiles TypeScript
  # without typechecking it, so the suites alone never see a type error.
  cd apps/minds/frontend && pnpm install --frozen-lockfile && pnpm run generate && pnpm run check && pnpm test


# Type-check and test apps/minds_evals. It is a standalone uv project, not a
# workspace member (see the root [tool.uv.workspace].exclude), so it has its own
# lock and venv: the root `uv sync --all-packages`, `just test-quick`, `just
# test-offload` and the root `ty check` all skip it, and this recipe is the only
# thing that runs its suite. `--locked` fails rather than silently re-resolving,
# so a pyproject edit without a matching `uv lock` is caught here.
[group("minds evals")]
test-minds-evals args="":
  cd apps/minds_evals && uv sync --locked && uv run ty check && uv run pytest {{args}}


# Render one relay's on-disk config (frps.toml, nftables.conf, the :80
# redirector) into OUT_DIR. The deploy recipe copies these onto the VPS; see
# apps/share_relay/README.md. RELAY_ID comes from `just register-share-relay`
# (or `minds-admin relays list`); CONTENT_DOMAIN is the env's content
# apex (imbueminds.com / minds-staging.com / minds-dev.com); PLUGIN_AUTH_URL is
# the connector's /frps/auth endpoint for that env.
[group("share-relay ops")]
render-share-relay relay_id region content_domain plugin_auth_url out_dir:
  uv run share-relay render --relay-id {{relay_id}} --region {{region}} \
    --content-domain {{content_domain}} --plugin-auth-url {{plugin_auth_url}} --out-dir {{out_dir}}

# Create one relay instance on OVH Public Cloud (reads OVH_* + OVH_CLOUD_PROJECT_ID
# from the env; values live in Vault under secrets/minds/<tier>/ovh). Regions
# run several relays; ordinal picks which one this is (names the instance
# share-relay-<env>-<region>-<n>).
[group("share-relay ops")]
provision-share-relay env_name region ordinal ovh_region ssh_public_key_file:
  uv run share-relay provision --env-name {{env_name}} --region {{region}} \
    --ordinal {{ordinal}} --ovh-region {{ovh_region}} --ssh-public-key-file {{ssh_public_key_file}}

# Register a relay in the connector's fleet inventory (the final provisioning
# step; reads MINDS_ADMIN_KEY from the env). Prints the relay record with its
# minted relay_id -- deploy-share-relay needs that id.
[group("share-relay ops")]
register-share-relay connector_url region tunnel_endpoint ip instance_name="":
  uv run share-relay register --connector-url {{connector_url}} --region {{region}} \
    --tunnel-endpoint {{tunnel_endpoint}} --ip {{ip}} --instance-name "{{instance_name}}"

# Retire a relay from the connector's fleet inventory (reads MINDS_ADMIN_KEY).
[group("share-relay ops")]
deregister-share-relay connector_url relay_id:
  uv run share-relay deregister --connector-url {{connector_url}} --relay-id {{relay_id}}

# Install/refresh a relay host's software + config (pinned frps, nftables,
# :80 redirector, healthcheck) and restart its services. relay_id comes from
# `just register-share-relay` (or `minds-admin relays list`);
# plugin_auth_url must include the shared-secret path segment:
# https://<connector>/frps/auth/<secret>
# (the secret lives in Vault under secrets/minds/<tier>/sharing/FRPS_AUTH_SECRET).
[group("share-relay ops")]
deploy-share-relay host relay_id region content_domain plugin_auth_url:
  uv run share-relay deploy --host {{host}} --relay-id {{relay_id}} --region {{region}} \
    --content-domain {{content_domain}} --plugin-auth-url {{plugin_auth_url}}

# Reconcile the region's DNS record set: relay.<region>.<domain> + *.<region>.<domain>
# gray-cloud A records covering EVERY relay IP in the region (pass --ip per relay via
# ips="ip1 ip2"). Bring-up / disaster-recovery path; the connector's health sweep
# maintains the same records in steady state. Reads CLOUDFLARE_API_TOKEN +
# CLOUDFLARE_ZONE_ID from the env.
[group("share-relay ops")]
dns-share-relay region content_domain +ips:
  uv run share-relay dns --region {{region}} --content-domain {{content_domain}} \
    $(for ip in {{ips}}; do printf -- "--ip %s " "$ip"; done)

# List / destroy relay instances in the OVH Public Cloud project.
[group("share-relay ops")]
list-share-relays:
  uv run share-relay list

[group("share-relay ops")]
destroy-share-relay instance_id:
  uv run share-relay destroy --instance-id {{instance_id}}

# Run the share_relay test suite.
[group("share-relay test")]
test-share-relay:
  uv run pytest apps/share_relay

# Regenerate the committed hash-locked image_requirements.txt exports that the
# Modal service images (remote_service_connector, modal_litellm) install from.
# Run after changing an app's [dependency-groups] image pins or relocking
# uv.lock; per-app drift tests and the `minds-admin env deploy` preflight fail until
# the committed exports match uv.lock again.
[group("mngr dev")]
export-image-requirements:
    uv run python -c "from pathlib import Path; from imbue.imbue_common.modal_image_requirements import IMAGE_PINNED_PACKAGE_NAMES; from imbue.modal_app_kit.testing import regenerate_image_requirements; print('\n'.join(str(p) for p in regenerate_image_requirements(Path.cwd(), IMAGE_PINNED_PACKAGE_NAMES)))"

# Diffs against the real base branch, so it must run on a real checkout
# (locally or the GitHub Actions runner), NOT inside an offload sandbox -- the
# sandbox has no base ref and the check would pass vacuously. Bare `python`
# (no `uv run`) because the gate is deliberately stdlib-only: no `uv sync`,
# matching how the `check-changelog` CI job invokes it.
# Check that this branch has a changelog entry per project it touches.
[group("mngr dev")]
check-changelog:
    python -m scripts.check_changelog_entries

# Ops recipes live in private.just, which is absent on the public mirror.
import? 'private.just'
