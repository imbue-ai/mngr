# minds deployment tests

## Overview

- New operator-invoked orchestrator (`just minds-test-deployment`) that stands up one or more shared dev environments, runs two parallel offload-Modal batches of pytest tests (one against deployed services, one that mints its own ephemeral envs to exercise the deploy path), and reliably cleans up every resource it creates -- both within the run and across runs.
- Two new pytest marks split the test surface by launch condition: `minds_deployment` for tests that exercise the deploy process itself (ephemeral env per test), `minds_services` for tests that hit a pre-stood-up shared env's deployed Modal apps + Neon + SuperTokens. The existing acceptance / release offload jobs exclude both marks so the normal CI matrix is unaffected.
- Resource lifecycle uses a real per-run ledger (`.minds/ci-test-deploys.jsonl`) for fast iteration cleanup plus a cross-operator name+age sweep (`ci-<YYYYMMDDTHHMMSSZ>` prefix, 4h staleness) as the authoritative safety net. Same shape as the existing `mngr_modal` and Docker container leak-detection patterns.
- Defers GitHub Actions integration entirely -- the orchestrator is operator-invoked from a workstation that has already run `vault login`, mirroring `minds env deploy` today. The orchestrator threads `VAULT_TOKEN` (and the resolved per-env secrets bundle) into each offload sandbox so in-sandbox `minds env deploy` calls work without further auth.
- Ships a small initial suite (~3-6 end-to-end tests) covering the highest-value live flows (deploy/destroy, sign-up + verify + sign-in, pool host bake/lease/release, Cloudflare tunnel create/destroy, litellm key + live LLM call, deploy -> auto-rollback -> deploy via an injected broken `/healthcheck`). All plumbing is sized so adding more tests later does not materially grow wall time.

## Expected Behavior

### Orchestrator invocations

- `just minds-test-deployment` (default run mode): runs the name+age sweep, pushes the FCT test branch, kicks off the `minds_deployment` offload batch immediately, deploys each configured shared env in parallel, kicks off the `minds_services` offload batch once shared envs are healthy, waits on both batches, tears down everything written to this run's ledger entries, then drops those entries from the ledger file (removing the file entirely if it ends up empty). Exits non-zero on any test failure OR any cleanup failure. Total wall time approximates `max(deployment-batch, shared-env-deploy + services-batch)`.
- `just minds-test-deployment --keep-on-failure`: same flow, but any test failure flips the test's ephemeral-env ledger entry to `status=leaked` so the end-of-run pass skips it. The operator inspects the deployed state directly; the next `--cleanup` invocation or the next-run age sweep reclaims it.
- `just minds-test-deployment-cleanup`: walks every ledger entry across all prior runs, tears each down (idempotent against already-destroyed resources), removes the ledger file once drained. Independent of any test run.
- `just minds-test-deployment-up <role>` / `just minds-test-deployment-down`: local iterate mode. `up` deploys the named shared env, writes its URLs + the FCT branch ref to `.minds/iterate-<role>.json`, prints a ready-to-paste `MINDS_DEPLOYMENT_TEST_ENVS_JSON=... uv run pytest ...` invocation, exits. `down` reads the state file and tears the env down (and deletes its FCT branch).
- `just minds-test-services-against <env-name> <test...>`: points the `minds_services` tests at any already-deployed dev env (e.g. the operator's `dev-josh`) and runs them locally via `uv run pytest` -- no offload, no env creation, no cleanup of the target env. The FCT branch lifecycle still runs by default so the tests can launch real agents; a `--no-fct-push` flag suppresses it for purely backend tests.

### Operator's git state

- After any orchestrator invocation that touches FCT, the operator's `~/project/forever-claude-template` checkout ends on the same branch + commit it started on, with the same set of tracked-modified and untracked files restored exactly as they were. The `ci-<timestamp>` branch exists only on the FCT remote until cleanup (or the age sweep) deletes it. A mid-flight crash leaves the operator's changes recoverable via `git stash list` -- they are never lost.

### Inside offload sandboxes

- Each sandbox starts with `test-results/deployment_envs.json` written by the orchestrator into the sandbox project root, containing the shared-env URL map, the FCT branch ref + remote URL, and the run id. Secrets are threaded in as env vars: per-shared-env Neon DSN + SuperTokens admin key, dev-tier Anthropic key, plus `VAULT_TOKEN` / `VAULT_ADDR` / `VAULT_NAMESPACE`. Tests acquire what they need via four fixtures: `shared_env(role=...)`, `fct_template_ref`, `verified_user`, `ephemeral_env`.
- The `vault` CLI is available inside the sandbox (installed via the shared mngr Dockerfile), so the in-sandbox `minds env deploy` subprocess invoked by the `ephemeral_env` fixture works without additional setup.

### Resource lifecycle

- Every CI-created cloud resource is named with the `ci-<YYYYMMDDTHHMMSSZ>` prefix the existing `secret_lifecycle` timestamp logic already understands: shared envs use the bare timestamp form (e.g. `ci-20260518T140212Z`), ephemeral envs append a short uuid (`ci-20260518T140530Z-a3f1`). FCT branches follow the same `ci-<timestamp>` shape.
- Every cloud resource the orchestrator creates is recorded in the per-run ledger at create time as `status=active` and flipped to `status=destroyed` on successful teardown (or `status=leaked` if `--keep-on-failure` opts to retain). Records carry `{kind: env|fct_branch, name, created_at, run_id, status}`.
- The orchestrator's name+age sweep at every run-start enumerates dev envs (`minds env list` shape) and FCT branches (`git ls-remote refs/heads/ci-*` on the FCT remote), parses the embedded timestamp, and destroys anything older than 4 hours regardless of which operator created it.
- Per-test SuperTokens users are **not** in the ledger -- the function-scoped `verified_user` fixture deletes them via the admin API for intra-run hygiene, and the shared env's entire SuperTokens app is destroyed at run end, so any leftover users go with it.

### Test-behavior interactions

- The deploy -> rollback -> deploy test exercises real auto-rollback wiring rather than the manual `modal app rollback` path. v1 deploys normally; v2 deploys with `MINDS_INJECT_BROKEN_HEALTHCHECK=1` as a per-deploy Modal Secret value, which the deployed `remote_service_connector` checks per request so its `/healthcheck` returns 500 unconditionally; `await_apps_healthy` fails, the deploy step calls `rollback_modal_app`, and the test asserts the Modal app version reverted to v1's id. v3 omits the env var and deploys cleanly. If today's `minds env deploy` does not already call `rollback_modal_app` on `await_apps_healthy` failure, that small wiring change ships in this PR; otherwise no production-code change beyond the connector's `/healthcheck` env-var read.
- The existing `offload-modal.toml`, `offload-modal-acceptance.toml`, and `offload-modal-release.toml` filter strings gain `and not minds_deployment and not minds_services` so the standard CI matrix never collects these tests (which would fail without the operator-side env setup).
- The `verified_user` fixture's per-test admin-API calls create users in the shape `test-<uuid>@example.test` -- predictable enough that an operator can spot them in the SuperTokens dashboard while debugging without colliding with any real account convention.

### Open questions deferred to follow-ups

- OAuth-based account creation testing (no stub file shipped in this PR).
- Live Neon / Prisma migration tests (today covered only by existing unit tests for `apply_pool_hosts_migrations`).
- GitHub Actions CI integration (blocked on solving vault-in-runner; the script and tests are designed so adding CI later is a small wrapper layer, not a redesign).
- Live latency / performance assertions (the suite produces deployed envs that could host them, but no perf tests are in the initial roster).

## Changes

### Orchestrator + recipes

- New `apps/minds/scripts/test_deployments.py` -- click-driven entrypoint owning FCT branch lifecycle, shared-env deploys, the two-batch offload dispatch, ledger reads/writes, per-run cleanup, paired cleanup, and the `up` / `down` / `against` local modes.
- New `just` recipes: `minds-test-deployment`, `minds-test-deployment-cleanup`, `minds-test-deployment-up`, `minds-test-deployment-down`, `minds-test-services-against` -- thin wrappers around the script.

### Pytest suite

- New directory `apps/minds/deployment_tests/` holding the initial test files split by feature (one file per: account lifecycle, tunnels, pool hosts, litellm, deploy/destroy, deploy/rollback). Each file declares `pytestmark = pytest.mark.minds_deployment` or `pytest.mark.minds_services`.
- New `apps/minds/deployment_tests/conftest.py` providing the four shared fixtures (`shared_env(role)`, `fct_template_ref`, `verified_user`, `ephemeral_env`).
- Both new marks registered in `libs/imbue_common/imbue/imbue_common/conftest_hooks.py`.

### Offload configs + Dockerfile

- New `offload-modal-minds-deployment.toml` -- filters `-m minds_deployment`, low `max_parallel` default (single-digit), reuses the shared mngr Dockerfile.
- New `offload-modal-minds-services.toml` -- filters `-m minds_services`, same shape.
- The shared `libs/mngr/imbue/mngr/resources/Dockerfile` gains an apt install of the `vault` CLI.
- The existing `offload-modal.toml`, `offload-modal-acceptance.toml`, `offload-modal-release.toml` filter strings exclude both new marks.

### Possible small production-code wiring

- If `minds env deploy` does not already call `rollback_modal_app` when `await_apps_healthy` fails, add that call to `apps/minds/imbue/minds/envs/provisioning.py`. Verified before implementation.
- The deployed `remote_service_connector` `/healthcheck` handler grows a per-request check for `MINDS_INJECT_BROKEN_HEALTHCHECK=1` that returns 500 when set. No other code paths affected; the env var is unset in every non-test deploy.

### State files / artifacts (no schema migrations)

- Ledger: `.minds/ci-test-deploys.jsonl` (repo-relative, gitignored), append-only JSONL with `{kind: env|fct_branch, name, created_at, run_id, status}`.
- Per-batch in-sandbox map: `test-results/deployment_envs.json` (written by the orchestrator before each offload invocation) containing `{shared_envs: {role: {connector_url, litellm_proxy_url, secrets: {...}}}, fct_test_branch, fct_test_remote, run_id}`.
- Local iterate state file: same shape as `deployment_envs.json`, written under `.minds/iterate-<role>.json` by `up`, consumed by `down`.

### Docs + changelog

- This spec at `specs/minds-deployment-tests.md`.
- `apps/minds/README.md` gains a short "Testing live deployments" section pointing at the spec.
- Existing `apps/minds/docs/environments.md` and `apps/minds/docs/vault-setup.md` are unchanged (operator vault flow is unchanged).
- New `changelog/mngr-minds-good-testing.md` summarizing the user-visible changes.
