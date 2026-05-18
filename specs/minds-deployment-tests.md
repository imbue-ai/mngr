# minds deployment tests

## Overview

- New operator-invoked orchestrator (`just minds-test-deployment`) that stands up one or more shared dev environments, runs two parallel offload-Modal batches of pytest tests (one against deployed services, one that mints its own ephemeral envs to exercise the deploy path), and reliably cleans up every resource it creates -- both within the run and across runs.
- Two new pytest marks split the test surface by launch condition: `minds_deployment` for tests that exercise the deploy process itself (ephemeral env per test), `minds_services` for tests that hit a pre-stood-up shared env's deployed Modal apps + Neon + SuperTokens. The existing acceptance / release offload jobs exclude both marks so the normal CI matrix is unaffected.
- Resource lifecycle uses a real per-run ledger (`.minds/ci-test-deploys.jsonl`) for fast iteration cleanup plus a cross-operator name+age sweep (`ci-<YYYYMMDDTHHMMSSZ>` prefix, 4h staleness) as the authoritative safety net. Same shape as the existing `mngr_modal` and Docker container leak-detection patterns.
- Defers GitHub Actions integration entirely -- the orchestrator is operator-invoked from a workstation that has already run `vault login`, mirroring `minds env deploy` today. The orchestrator threads `VAULT_TOKEN` (and the resolved per-env secrets bundle) into each offload sandbox so in-sandbox `minds env deploy` calls work without further auth.
- Ships an initial suite of three `minds_deployment` tests (full create/destroy round-trip, auto-rollback on broken `/healthcheck`, re-deploy advances version) and three `minds_services` tests (realistic signup + email-verify-via-mail.tm + tunnel lifecycle, logged-in smoke across customer-facing routes, real-LLM-call-through-litellm via a local Docker FCT workspace asserting spend lands in Neon). One bigger pool-host bake/lease/agent/release-plus-user-isolation test is explicitly deferred to a follow-up PR. All plumbing is sized so adding more tests later does not materially grow wall time.

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

- Each sandbox starts with `test-results/deployment_envs.json` written by the orchestrator into the sandbox project root, containing the shared-env URL map, the FCT branch ref + remote URL, and the run id. Secrets are threaded in as env vars: per-shared-env Neon DSN + SuperTokens admin key, dev-tier Anthropic key, the per-run mail.tm test account credentials (address + JWT) for signup-flow tests, plus `VAULT_TOKEN` / `VAULT_ADDR` / `VAULT_NAMESPACE`. Tests acquire what they need via five fixtures: `shared_env(role=...)`, `fct_template_ref`, `verified_user`, `ephemeral_env`, `signup_email` (returns a fresh `+<uuid>` address against the shared mail.tm account plus a `wait_for_verification_token()` helper).
- The `vault` CLI is available inside the sandbox (installed via the shared mngr Dockerfile), so the in-sandbox `minds env deploy` subprocess invoked by the `ephemeral_env` fixture works without additional setup. The `minds_services` offload config additionally enables Docker-in-Docker (`enable_docker = true`, matching `offload-modal-acceptance.toml`) so the litellm-via-workspace test can spin up a local FCT container inside the sandbox.

### Resource lifecycle

- Every CI-created cloud resource is named with the `ci-<YYYYMMDDTHHMMSSZ>` prefix the existing `secret_lifecycle` timestamp logic already understands: shared envs use the bare timestamp form (e.g. `ci-20260518T140212Z`), ephemeral envs append a short uuid (`ci-20260518T140530Z-a3f1`). FCT branches follow the same `ci-<timestamp>` shape.
- Every cloud resource the orchestrator creates is recorded in the per-run ledger at create time as `status=active` and flipped to `status=destroyed` on successful teardown (or `status=leaked` if `--keep-on-failure` opts to retain). Records carry `{kind: env|fct_branch|mailtm_account, name, created_at, run_id, status}`.
- The orchestrator's name+age sweep at every run-start enumerates dev envs (`minds env list` shape) and FCT branches (`git ls-remote refs/heads/ci-*` on the FCT remote), parses the embedded timestamp, and destroys anything older than 4 hours regardless of which operator created it.
- Per-test SuperTokens users are **not** in the ledger -- the function-scoped `verified_user` fixture deletes them via the admin API for intra-run hygiene, and the shared env's entire SuperTokens app is destroyed at run end, so any leftover users go with it.

### Test-behavior interactions

- The auto-rollback test (`test_deploy_auto_rollback_on_broken_healthcheck`) exercises real auto-rollback wiring rather than the manual `modal app rollback` path. v2 deploys with `MINDS_INJECT_BROKEN_HEALTHCHECK=1` as a per-deploy Modal Secret value, which the deployed `remote_service_connector` checks per request so its `/healthcheck` returns 500 unconditionally; `await_apps_healthy` fails, the deploy step calls `rollback_modal_app`, and the test asserts the Modal app version reverted to v1's id. The test does NOT redeploy a v3 -- the steady-state "redeploy advances version" contract is covered separately by `test_deploy_new_version`. If today's `minds env deploy` does not already call `rollback_modal_app` on `await_apps_healthy` failure, that small wiring change ships in this PR; otherwise no production-code change beyond the connector's `/healthcheck` env-var read.
- The existing `offload-modal.toml`, `offload-modal-acceptance.toml`, and `offload-modal-release.toml` filter strings gain `and not minds_deployment and not minds_services` so the standard CI matrix never collects these tests (which would fail without the operator-side env setup).
- The `verified_user` fixture's per-test admin-API calls create users in the shape `test-<uuid>@example.test` -- predictable enough that an operator can spot them in the SuperTokens dashboard while debugging without colliding with any real account convention.

### Initial test inventory

#### `minds_deployment` (all three shipped in this PR)

- `test_deploy_then_destroy_round_trip`
  - Deploy from clean. Assert: Modal env exists, both Modal apps deployed and `/healthcheck` returns 200, Neon project exists with `host_pool` + `litellm_cost` DBs, SuperTokens app exists, `client.toml` + `secrets.toml` written under the env root, generation id present in Vault.
  - Destroy. Assert: every resource above is gone, env root removed, no resources tagged with this env name found by per-provider enumeration.
  - The `ephemeral_env` fixture's teardown is a no-op against an env that has already been destroyed (uses the same env-root presence check `minds env destroy` itself relies on), so a successful test does not double-destroy or crash on missing state.

- `test_deploy_auto_rollback_on_broken_healthcheck`
  - Deploy v1 normally; capture v1 Modal app version id.
  - Deploy v2 with `MINDS_INJECT_BROKEN_HEALTHCHECK=1` threaded into the connector's deploy-secret bundle.
  - Assert: `minds env deploy` exits non-zero, the live Modal app version is back at v1's id, `/healthcheck` returns 200.
  - Does not redeploy a v3 -- the "redeploy advances version" contract is `test_deploy_new_version`'s job.

- `test_deploy_new_version`
  - Deploy v1 to a fresh ephemeral env; capture v1 Modal app version id.
  - Deploy v2 against the same env (with one trivially-different deploy-secret env var, since each deploy mints a fresh `MINDS_DEPLOY_ID` automatically the env-var change is more illustrative than load-bearing).
  - Assert: live Modal app version advanced past v1's id; the connector's `/version` endpoint reflects the new `MINDS_DEPLOY_ID`.
  - Covers the steady-state contract that re-deploying actually deploys.

#### `minds_services` (three shipped in this PR + one deferred)

- `test_realistic_signup_verify_signin_create_tunnel_signout`
  - Sign up via the connector's public sign-up endpoint with `signup_email` fixture's fresh `+<uuid>` address against the per-run shared mail.tm account.
  - Poll mail.tm's HTTP API for the verification email; extract the verify token from the body; POST it to the connector's `/verify-email` endpoint.
  - Sign in with the credentials; assert session cookie/token returned.
  - Create a Cloudflare tunnel via the connector's tunnel-management endpoint; assert it exists in Cloudflare (list, filtered by env tag).
  - Hit the tunnel URL; assert it routes (to a minimal "is-this-on" backend the test stands up locally inside the sandbox, or assert the well-known 4xx shape for an unrouted tunnel).
  - Delete the tunnel; assert gone from Cloudflare.
  - Sign out; assert subsequent requests with the same session token return 401.

- `test_logged_in_smoke`
  - Minimal smoke that deployed services + routes respond as expected for a logged-in user. Uses the `verified_user` fixture (admin-bypass verification, since the realistic verify-email path is already covered by the signup test).
  - Hits each connector route the desktop client uses on the home screen (agent list, host list, settings, `/version`) plus the litellm-proxy's `/health`; asserts each returns the expected shape + 2xx.
  - Cheap, fast signal that distinguishes "env is sick" from "a specific feature broke" when one of the heavier tests fails.

- `test_litellm_spend_tracking_via_local_workspace`
  - Real-product test of "minds agent uses imbue_cloud LLM and spend is tracked".
  - Inside the offload sandbox, `mngr create` a new local Docker container running the FCT template (using the orchestrator-pushed FCT branch ref from `fct_template_ref`), configured with the `imbue_cloud` AI-key option so the agent's LLM calls flow through the shared env's `litellm_proxy_url`.
  - Use `mngr message` to send a real chat message to claude inside the container.
  - Assert: claude responds in the container's transcript within a reasonable timeout (message actually got sent + processed).
  - Query the shared env's Neon `litellm_cost` DB; assert a row exists for this run's litellm key with non-zero spend within the last few seconds.
  - Requires Docker-in-Docker in the offload sandbox (enabled in `offload-modal-minds-services.toml`).

- **Deferred to a follow-up PR:** `test_bake_lease_create_agent_release_pool_host_with_user_isolation`
  - Bakes an OVH VPS as a pool host, creates two `verified_user`s, leases the host as user A, creates an FCT agent on it (asserts the agent boots), asserts user B cannot see the host or its agent via the connector's user-facing API (404, not 403), destroys the agent, releases the host. Final fixture teardown destroys the OVH instance.
  - Combines pool-host lifecycle and user-isolation because the host is the only user-facing resource that meaningfully surfaces in the API for the isolation assertion.
  - Deferred because OVH bakes take minutes + cost real money per iteration, so the dev loop is painful while debugging this test for the first time. All shared-env, FCT, and ledger plumbing is built so this is purely an "add the test file" follow-up, not a redesign.

### Open questions deferred to follow-ups

- The pool-host bake/lease/agent/release-plus-user-isolation test enumerated above.
- OAuth-based account creation testing (no stub file shipped in this PR).
- Live Neon / Prisma migration tests (today covered only by existing unit tests for `apply_pool_hosts_migrations`).
- GitHub Actions CI integration (blocked on solving vault-in-runner; the script and tests are designed so adding CI later is a small wrapper layer, not a redesign).
- Live latency / performance assertions (the suite produces deployed envs that could host them, but no perf tests are in the initial roster).
- Many shorter, narrower tests of individual surfaces (the initial six are deliberately heavy end-to-end flows; finer-grained tests against the same shared env are cheap to add later).

## Changes

### Orchestrator + recipes

- New `apps/minds/scripts/test_deployments.py` -- click-driven entrypoint owning FCT branch lifecycle, shared-env deploys, the two-batch offload dispatch, ledger reads/writes, per-run cleanup, paired cleanup, and the `up` / `down` / `against` local modes.
- New `just` recipes: `minds-test-deployment`, `minds-test-deployment-cleanup`, `minds-test-deployment-up`, `minds-test-deployment-down`, `minds-test-services-against` -- thin wrappers around the script.

### Pytest suite

- New directory `apps/minds/deployment_tests/` holding the six initial test files:
  - `test_deploy_round_trip.py` (`minds_deployment`)
  - `test_deploy_rollback.py` (`minds_deployment`)
  - `test_deploy_new_version.py` (`minds_deployment`)
  - `test_signup_tunnel.py` (`minds_services`)
  - `test_logged_in_smoke.py` (`minds_services`)
  - `test_litellm_via_workspace.py` (`minds_services`)
  Each file declares `pytestmark = pytest.mark.minds_deployment` or `pytest.mark.minds_services`.
- New `apps/minds/deployment_tests/conftest.py` providing the five shared fixtures (`shared_env(role)`, `fct_template_ref`, `verified_user`, `ephemeral_env`, `signup_email`).
- Both new marks registered in `libs/imbue_common/imbue/imbue_common/conftest_hooks.py`.

### Offload configs + Dockerfile

- New `offload-modal-minds-deployment.toml` -- filters `-m minds_deployment`, low `max_parallel` default (single-digit), reuses the shared mngr Dockerfile.
- New `offload-modal-minds-services.toml` -- filters `-m minds_services`, same shape, plus `enable_docker = true` under `[provider.experimental_options]` and the corresponding `MODAL_IMAGE_BUILDER_VERSION=2025.06` in its `just` recipe so the litellm-via-workspace test can spin up local FCT containers inside the sandbox (mirrors how `offload-modal-acceptance.toml` does it today).
- The shared `libs/mngr/imbue/mngr/resources/Dockerfile` gains an apt install of the `vault` CLI.
- The existing `offload-modal.toml`, `offload-modal-acceptance.toml`, `offload-modal-release.toml` filter strings exclude both new marks.

### Possible small production-code wiring

- If `minds env deploy` does not already call `rollback_modal_app` when `await_apps_healthy` fails, add that call to `apps/minds/imbue/minds/envs/provisioning.py`. Verified before implementation.
- The deployed `remote_service_connector` `/healthcheck` handler grows a per-request check for `MINDS_INJECT_BROKEN_HEALTHCHECK=1` that returns 500 when set. No other code paths affected; the env var is unset in every non-test deploy.
- The deployed `remote_service_connector` exposes a `/version` endpoint that returns `{"deploy_id": MINDS_DEPLOY_ID, "generation_id": MINDS_TIER_GENERATION_ID}` for `test_deploy_new_version` and `test_logged_in_smoke` to assert against. Public, unauthenticated (mirrors the existing `/generation`); reuses values the connector already reads at module load.

### External test infrastructure

- mail.tm is added as the verification mailbox for the realistic signup test. The orchestrator creates one disposable mail.tm account per run via the public mail.tm HTTP API, threads its address + JWT into every `minds_services` sandbox as env vars (`MAILTM_ACCOUNT_ADDRESS`, `MAILTM_ACCOUNT_JWT`), and deletes the account in end-of-run cleanup (tracked in the ledger as `kind=mailtm_account` -- extends the schema from `{kind: env|fct_branch, ...}` to `{kind: env|fct_branch|mailtm_account, ...}`). Per-test signups use `+<uuid>` local-part suffixes against that shared address so no per-test mail.tm account creation is needed.
- The `signup_email` fixture wraps a tiny mail.tm HTTP client (no SDK -- ~50 lines of `httpx`); placed under `apps/minds/deployment_tests/_mailtm.py` so it can be reused by future signup-flow tests.

### State files / artifacts (no schema migrations)

- Ledger: `.minds/ci-test-deploys.jsonl` (repo-relative, gitignored), append-only JSONL with `{kind: env|fct_branch|mailtm_account, name, created_at, run_id, status}`.
- Per-batch in-sandbox map: `test-results/deployment_envs.json` (written by the orchestrator before each offload invocation) containing `{shared_envs: {role: {connector_url, litellm_proxy_url, secrets: {...}}}, fct_test_branch, fct_test_remote, run_id}`.
- Local iterate state file: same shape as `deployment_envs.json`, written under `.minds/iterate-<role>.json` by `up`, consumed by `down`.

### Docs + changelog

- This spec at `specs/minds-deployment-tests.md`.
- `apps/minds/README.md` gains a short "Testing live deployments" section pointing at the spec.
- Existing `apps/minds/docs/environments.md` and `apps/minds/docs/vault-setup.md` are unchanged (operator vault flow is unchanged).
- New `changelog/mngr-minds-good-testing.md` summarizing the user-visible changes.
