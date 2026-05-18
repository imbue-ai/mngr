# minds deployment tests

## Overview

- New operator-invoked orchestrator (`just minds-test-deployment`) that stands up one or more shared dev environments, runs both pytest batches strictly sequentially (one pytest invocation per mark, no xdist within either), and reliably cleans up every resource it creates -- both within the run and across runs. Initial implementation runs tests locally for fast debug-iterate cycles while the suite is being stabilized. Sequential is a deliberate constraint: multiple desktop-client instances against the same shared env are not safe today, and running multiple pytest processes concurrently against the same shared env shares the same hazard plus its own coordination complexity. Limiting to one pytest process at a time (and one in-process desktop client at a time) means tests can rely on that property until the desktop client itself is hardened. The orchestrator itself is plain Python (click-driven) -- not a pytest wrapper -- so the pre-test setup and post-test cleanup do not accidentally inherit pytest semantics.
- Two new pytest marks split the test surface by launch condition: `minds_deployment` for tests that exercise the deploy process itself (ephemeral env per test), `minds_services` for tests that hit a pre-stood-up shared env's deployed Modal apps + Neon + SuperTokens. The existing acceptance / release offload jobs (and the local `just test-unit` / `test-integration` / `test-quick` recipes) exclude both marks so neither the normal CI matrix nor a plain local `pytest` run accidentally collects them.
- Resource lifecycle uses a real per-run ledger (`.minds/ci-test-deploys.jsonl`) for fast iteration cleanup plus a cross-operator name+age sweep (`ci-<YYYYMMDDTHHMMSSZ>` prefix, 4h staleness) as the authoritative safety net. Same shape as the existing `mngr_modal` and Docker container leak-detection patterns.
- Defers GitHub Actions integration entirely -- the orchestrator is operator-invoked from a workstation that has already run `vault login`, mirroring `minds env deploy` today. The orchestrator sets up the pytest process's env (`VAULT_TOKEN`, the resolved per-env secrets bundle, the mail.tm credentials, etc.) before invoking pytest, so in-process `minds env deploy` calls work without further auth. The same env-var contract applies when this moves to offload later.
- Ships an initial suite of three `minds_deployment` tests (full create/destroy round-trip, auto-rollback on broken `/healthcheck`, re-deploy advances version) and three `minds_services` tests (realistic signup + email-verify-via-mail.tm + tunnel lifecycle, logged-in smoke across customer-facing routes, real-LLM-call-through-litellm via a local Docker FCT workspace asserting spend lands in Neon). One bigger pool-host bake/lease/agent/release-plus-user-isolation test is explicitly deferred to a follow-up PR. All plumbing is sized so adding more tests later does not materially grow wall time.

## Expected Behavior

### Orchestrator invocations

- `just minds-test-deployment` (default run mode): runs the name+age sweep, verifies the FCT worktree exists at `<monorepo>/.external_worktrees/forever-claude-template/` (errors out with a setup pointer if missing -- the orchestrator does not create the worktree for you), pushes the worktree's current state to a fresh `ci-<timestamp>` branch on the FCT remote (stash + commit + push + restore, all inside the worktree -- never touches the operator's primary `~/project/forever-claude-template` clone) so the same flow is exercised today that the future offload move will require, creates the per-run mail.tm account, deploys each configured shared env one at a time (serial; the initial roster is a single `default` env so concurrency isn't a win yet), invokes pytest twice in sequence -- first `uv run pytest -m minds_deployment ...` (one-shot, blocks to completion), then `uv run pytest -m minds_services ...` (also blocks to completion) -- tears down everything written to this run's ledger entries (including deleting the pushed FCT branch from the remote), then drops those entries from the ledger file (removing the file entirely if it ends up empty). Exits non-zero on any test failure OR any cleanup failure. Total wall time approximates `shared-env-deploy + deployment-batch + services-batch` -- noticeably slower than a hypothetical parallel design, but parallelism comes back for free when the suite moves to offload (where each test gets its own sandbox + can run its own desktop client safely).
- `just minds-test-deployment --keep-on-failure`: same flow, but any test failure flips the test's ephemeral-env ledger entry to `status=leaked` so the end-of-run pass skips it. The operator inspects the deployed state directly; the next `--cleanup` invocation or the next-run age sweep reclaims it.
- `just minds-test-deployment-cleanup`: walks every ledger entry across all prior runs, tears each down (idempotent against already-destroyed resources), removes the ledger file once drained. Independent of any test run.
- `just minds-test-deployment-up <role>` / `just minds-test-deployment-down`: local iterate mode. `up` deploys the named shared env, performs the FCT worktree push (same as the default run mode), writes the resulting URLs + the worktree path + the pushed FCT branch ref to `.minds/iterate-<role>.json`, prints a ready-to-paste `MINDS_DEPLOYMENT_TEST_ENVS_JSON=... uv run pytest ...` invocation, exits. `down` reads the state file and tears the env down (and deletes the pushed FCT branch from the remote).
- `just minds-test-services-against <env-name> <test...>`: points the `minds_services` tests at any already-deployed dev env (e.g. the operator's `dev-josh`) and runs them locally via `uv run pytest` -- no env creation, no cleanup of the target env. The FCT worktree push still runs by default so the tests can launch real agents; a `--no-fct-push` flag suppresses it for purely backend tests that do not create workspaces.

### Operator's git state

- The orchestrator's FCT branch flow runs entirely inside the worktree at `<monorepo>/.external_worktrees/forever-claude-template/`; the operator's primary `~/project/forever-claude-template` clone (if any) is never touched. The worktree must already exist -- per the CLAUDE.md convention, the operator creates it via `git worktree add` from their FCT clone, matching whichever FCT branch they're testing against. The orchestrator validates the worktree's presence at startup and errors out with the setup command if missing.
- After any orchestrator invocation that pushes an FCT branch, the worktree ends on the same branch + commit it started on, with the same set of tracked-modified and untracked files restored exactly as they were (via `git stash --include-untracked` → checkout `ci-<timestamp>` → `git stash apply` → commit + push → checkout original branch → `git stash pop`). The `ci-<timestamp>` branch exists only on the FCT remote until cleanup (or the age sweep) deletes it. A mid-flight crash leaves the operator's worktree changes recoverable via `git stash list` -- they are never lost.

### Inside the test process (local pytest now; offload-portable design)

- Before each pytest invocation, the orchestrator writes `test-results/deployment_envs.json` to a known path (current working directory locally; sandbox project root when this moves to offload), containing the shared-env URL map, the absolute path to the FCT worktree (`fct_worktree_path`), the pushed FCT branch ref (`fct_test_branch` + `fct_test_remote`), and the run id. Secrets are exported into the pytest process's env: per-shared-env Neon DSN + SuperTokens admin key, dev-tier Anthropic key, the per-run mail.tm test account credentials (address + JWT), plus `VAULT_TOKEN` / `VAULT_ADDR` / `VAULT_NAMESPACE`. Tests acquire what they need via five fixtures: `shared_env(role=...)`, `fct_template_ref` (returns the FCT worktree path today since tests run locally and the worktree is reachable on disk; when this moves to offload the same fixture will return the pushed `git_url@branch` form so sandboxes can clone it -- the fixture is the abstraction boundary, test code does not change), `verified_user`, `ephemeral_env`, `signup_email` (returns a fresh `+<uuid>` address against the shared mail.tm account plus `wait_for_verification_token()` and `wait_for_one_time_code()` helpers, since the real sign-in flow also goes through email).
- pytest itself is invoked with no xdist (no `-n`) -- tests within a pytest run execute strictly sequentially -- so each in-process desktop client created by a test exists alone at any moment, sidestepping the multi-client-against-same-env hazard. Adding xdist later would require either the desktop-client-per-test hazard going away or moving to offload (one sandbox per test).
- The `vault` CLI must already be installed on the operator's machine (the same prerequisite `minds env deploy` carries today). The orchestrator's startup checks this and prints a pointer to `apps/minds/docs/vault-setup.md` if missing. For the future offload move, the shared mngr Dockerfile will gain a `vault` apt install and the `minds_services` offload config will enable Docker-in-Docker so the litellm-via-workspace test can spin up a local FCT container inside the sandbox -- both noted as deferred work, not shipped in this PR.

### Resource lifecycle

- Every CI-created cloud resource is named with the `ci-<YYYYMMDDTHHMMSSZ>` prefix the existing `secret_lifecycle` timestamp logic already understands: shared envs use the bare timestamp form (e.g. `ci-20260518T140212Z`), ephemeral envs append a short uuid (`ci-20260518T140530Z-a3f1`). FCT branches follow the same `ci-<timestamp>` shape.
- Every cloud resource the orchestrator creates is recorded in the per-run ledger at create time as `status=active` and flipped to `status=destroyed` on successful teardown (or `status=leaked` if `--keep-on-failure` opts to retain). Records carry `{kind: env|fct_branch|mailtm_account, name, created_at, run_id, status}`.
- The orchestrator's name+age sweep at every run-start enumerates dev envs (`minds env list` shape) and FCT branches (`git ls-remote refs/heads/ci-*` against the FCT remote), parses the embedded timestamp, and destroys anything older than 4 hours regardless of which operator created it.
- Per-test SuperTokens users are **not** in the ledger -- the function-scoped `verified_user` fixture deletes them via the admin API for intra-run hygiene, and the shared env's entire SuperTokens app is destroyed at run end, so any leftover users go with it.

### Test-behavior interactions

- The auto-rollback test (`test_deploy_auto_rollback_on_broken_healthcheck`) exercises real auto-rollback wiring rather than the manual `modal app rollback` path. v2 deploys with `MINDS_INJECT_BROKEN_HEALTHCHECK=1` as a per-deploy Modal Secret value, which the deployed `remote_service_connector` checks per request so its `/healthcheck` returns 500 unconditionally; `await_apps_healthy` fails, the deploy step calls `rollback_modal_app`, and the test asserts the Modal app version reverted to v1's id. The test does NOT redeploy a v3 -- the steady-state "redeploy advances version" contract is covered separately by `test_deploy_new_version`. If today's `minds env deploy` does not already call `rollback_modal_app` on `await_apps_healthy` failure, that small wiring change ships in this PR; otherwise no production-code change beyond the connector's `/healthcheck` env-var read.
- The existing `offload-modal.toml`, `offload-modal-acceptance.toml`, and `offload-modal-release.toml` filter strings gain `and not minds_deployment and not minds_services`, and the local `just test-unit` / `test-integration` / `test-quick` recipes' shared `-m` filter gains the same exclusions, so neither the standard CI matrix nor a plain local `pytest` run accidentally collects these tests (which would fail without the operator-side env setup).
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
  - The full first-time-user flow end-to-end, exercising the only customer-facing path that goes through real email + the desktop client's workspace + tunnel surface in one test. Drives the connector + the desktop client programmatically (in-process `create_desktop_client(...)` -- same pattern as the existing `test_desktop_client_e2e.py` -- plus direct HTTP) rather than through a real Electron instance. Replacing this with Playwright against a packaged Electron is captured in Future Work below.
  - **Signup:** POST to the connector's public sign-up endpoint with `signup_email` fixture's fresh `+<uuid>` address (rooted at the per-run shared mail.tm account).
  - **Email verification:** poll mail.tm's HTTP API for the verification email, extract the verify token, POST it to the connector's `/verify-email` endpoint.
  - **Sign-in (one-time code):** trigger the connector's email-one-time-code sign-in flow (the real sign-in path -- no password); poll mail.tm again for the one-time-code email; submit the code to complete sign-in; assert session cookie/token returned.
  - **Workspace creation:** drive the in-process desktop client to create a workspace from the FCT template (using `fct_template_ref`); wait for it to reach a running state.
  - **Forward the system-interface:** drive the desktop client's "forward system-interface" action for that workspace -- this is the user-facing operation that creates the Cloudflare tunnel pointing at the workspace's system-interface port. Assert the tunnel exists in Cloudflare (list, filtered by env tag) AND that the forwarded URL serves the expected response when hit from the test process.
  - **Teardown:** stop forwarding (assert tunnel gone from Cloudflare), destroy the workspace, sign out, assert subsequent requests with the same session token return 401.

- `test_logged_in_smoke`
  - Minimal smoke that deployed services + routes respond as expected for a logged-in user. Uses the `verified_user` fixture (admin-bypass verification, since the realistic verify-email path is already covered by the signup test).
  - Hits each connector route the desktop client uses on the home screen (agent list, host list, settings, `/version`) plus the litellm-proxy's `/health`; asserts each returns the expected shape + 2xx.
  - Cheap, fast signal that distinguishes "env is sick" from "a specific feature broke" when one of the heavier tests fails.

- `test_litellm_spend_tracking_via_local_workspace`
  - Real-product test of "minds agent uses imbue_cloud LLM and spend is tracked".
  - Uses the `verified_user` fixture (admin-bypass verification + admin-minted session token, so the test does not redo the realistic signup flow).
  - Drives the in-process desktop client (same shape as test 1 -- programmatic, not Electron/Playwright) to create a local Docker workspace from the FCT template (using `fct_template_ref`) configured with the `imbue_cloud` AI-key option so the agent's LLM calls flow through the shared env's `litellm_proxy_url`.
  - Use `mngr message` (subprocess) against the running container to send a real chat message to claude inside.
  - Assert: claude responds in the container's transcript within a reasonable timeout (message actually got sent + processed).
  - Query the shared env's Neon `litellm_cost` DB; assert a row exists for this run's litellm key with non-zero spend within the last few seconds.
  - Runs locally today against the operator's Docker daemon. When this moves to offload, the future `offload-modal-minds-services.toml` enables Docker-in-Docker (mirroring `offload-modal-acceptance.toml`).

- **Deferred to a follow-up PR:** `test_bake_lease_create_agent_release_pool_host_with_user_isolation`
  - Bakes an OVH VPS as a pool host, creates two `verified_user`s, leases the host as user A, creates an FCT agent on it (asserts the agent boots), asserts user B cannot see the host or its agent via the connector's user-facing API (404, not 403), destroys the agent, releases the host. Final fixture teardown destroys the OVH instance.
  - Combines pool-host lifecycle and user-isolation because the host is the only user-facing resource that meaningfully surfaces in the API for the isolation assertion.
  - Deferred because OVH bakes take minutes + cost real money per iteration, so the dev loop is painful while debugging this test for the first time. All shared-env, FCT, and ledger plumbing is built so this is purely an "add the test file" follow-up, not a redesign.

### Future work (designed in, not shipped in this PR)

- **Offload-Modal parallelism.** The orchestrator runs both pytest batches locally and sequentially today. Moving to offload is what unblocks parallelism: each test inside its own sandbox gets its own desktop-client instance, so the multi-client-against-same-env hazard simply does not apply, and many tests can run concurrently. Mechanically it is a wrapper change: write `offload-modal-minds-deployment.toml` + `offload-modal-minds-services.toml`, add the `vault` apt install + `enable_docker = true` to the shared mngr Dockerfile, switch the orchestrator from `uv run pytest` to `offload run`, and flip the `fct_template_ref` fixture from returning the worktree path to returning the pushed `git_url@branch` ref (already in `deployment_envs.json` and already in the ledger; no plumbing change). Postponed until the test count + wall time justify the operational complexity (and until the tests are stable enough that the slower offload feedback loop is acceptable).
- **Playwright + Electron driving** of `test_realistic_signup_verify_signin_create_tunnel_signout`, `test_litellm_spend_tracking_via_local_workspace`, and the deferred pool-host test. Today they drive the desktop client programmatically (in-process `create_desktop_client` plus direct HTTP). The future version uses Playwright against a packaged Electron instance through the same flows, catching the layer of bugs that only show up in the actual Electron runtime. Out of scope here -- programmatic driving is sufficient to surface the integration bugs we care about for the initial suite.

### Open questions deferred to follow-ups

- The pool-host bake/lease/agent/release-plus-user-isolation test enumerated above.
- OAuth-based account creation testing (no stub file shipped in this PR).
- Live Neon / Prisma migration tests (today covered only by existing unit tests for `apply_pool_hosts_migrations`).
- GitHub Actions CI integration (blocked on solving vault-in-runner; the script and tests are designed so adding CI later is a small wrapper layer, not a redesign).
- Live latency / performance assertions (the suite produces deployed envs that could host them, but no perf tests are in the initial roster).
- Many shorter, narrower tests of individual surfaces (the initial six are deliberately heavy end-to-end flows; finer-grained tests against the same shared env are cheap to add later).

## Changes

### Orchestrator + recipes

- New `apps/minds/scripts/test_deployments.py` -- plain-Python click-driven entrypoint (not a pytest wrapper) owning FCT worktree validation + branch push/restore lifecycle (operating on `<monorepo>/.external_worktrees/forever-claude-template/`), shared-env deploys, sequential dispatch of the two `uv run pytest` invocations (one per mark), ledger reads/writes, per-run cleanup, paired cleanup, and the `up` / `down` / `against` local modes.
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

### Test selection (shipped) + offload configs (deferred)

- The existing `offload-modal.toml`, `offload-modal-acceptance.toml`, `offload-modal-release.toml` filter strings gain `and not minds_deployment and not minds_services` so the standard CI matrix never collects these tests.
- The local `just test-unit` / `test-integration` / `test-quick` recipes' shared `-m` filter (`_skip_acceptance_and_release` in the justfile) gains the same exclusions, so a plain local `pytest` run does not collect them either.
- **Deferred to a follow-up PR (per Future work):** the new `offload-modal-minds-deployment.toml` + `offload-modal-minds-services.toml` configs, the `vault` apt install in the shared `libs/mngr/imbue/mngr/resources/Dockerfile`, and the Docker-in-Docker config for the services config. Designed into this spec for portability; not shipped now to keep initial scope debugger-friendly.

### Possible small production-code wiring

- If `minds env deploy` does not already call `rollback_modal_app` when `await_apps_healthy` fails, add that call to `apps/minds/imbue/minds/envs/provisioning.py`. Verified before implementation.
- The deployed `remote_service_connector` `/healthcheck` handler grows a per-request check for `MINDS_INJECT_BROKEN_HEALTHCHECK=1` that returns 500 when set. No other code paths affected; the env var is unset in every non-test deploy.
- The deployed `remote_service_connector` exposes a `/version` endpoint that returns `{"deploy_id": MINDS_DEPLOY_ID, "generation_id": MINDS_TIER_GENERATION_ID}` for `test_deploy_new_version` and `test_logged_in_smoke` to assert against. Public, unauthenticated (mirrors the existing `/generation`); reuses values the connector already reads at module load.

### External test infrastructure

- mail.tm is added as the verification + one-time-code mailbox for the realistic signup test. The orchestrator creates one disposable mail.tm account per run via the public mail.tm HTTP API, exports its address + JWT into the `minds_services` pytest process's env as `MAILTM_ACCOUNT_ADDRESS` + `MAILTM_ACCOUNT_JWT` (will become per-sandbox env vars in the future offload variant -- same contract), and deletes the account in end-of-run cleanup (tracked in the ledger as `kind=mailtm_account`). Per-test signups use `+<uuid>` local-part suffixes against that shared address so no per-test mail.tm account creation is needed.
- The `signup_email` fixture wraps a tiny mail.tm HTTP client (no SDK -- ~50 lines of `httpx`); placed under `apps/minds/deployment_tests/_mailtm.py` so it can be reused by future signup-flow tests.

### State files / artifacts (no schema migrations)

- Ledger: `.minds/ci-test-deploys.jsonl` (repo-relative, gitignored), append-only JSONL with `{kind: env|fct_branch|mailtm_account, name, created_at, run_id, status}`.
- Per-pytest-invocation map: `test-results/deployment_envs.json` (written by the orchestrator before each `uv run pytest` invocation -- and before each `offload run` invocation later) containing `{shared_envs: {role: {connector_url, litellm_proxy_url, secrets: {...}}}, fct_worktree_path, fct_test_branch, fct_test_remote, run_id}`.
- Local iterate state file: same shape as `deployment_envs.json`, written under `.minds/iterate-<role>.json` by `up`, consumed by `down`.

### Docs + changelog

- This spec at `specs/minds-deployment-tests.md`.
- `apps/minds/README.md` gains a short "Testing live deployments" section pointing at the spec.
- Existing `apps/minds/docs/environments.md` and `apps/minds/docs/vault-setup.md` are unchanged (operator vault flow is unchanged).
- New `changelog/mngr-minds-good-testing.md` summarizing the user-visible changes.
