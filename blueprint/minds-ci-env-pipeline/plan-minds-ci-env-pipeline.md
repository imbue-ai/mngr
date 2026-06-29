# Plan: per-run minds CI environment in the snapshot test pipeline

## Refined prompt

> we want to add another precursor CI stage (like the modal snapshot build one for minds) that creates a minds CI environment for that particular run (ie, git hash) that is being tested
>
> * Create exactly one ephemeral `ci-*` env per CI run in the new env-build stage, shared by all tests in the offload run, and destroyed after.
> * Keep the existing `ci-<timestamp>-<uuid>` naming (don't encode the git SHA) so the age sweep keeps working; pass the env name / cleanup handles forward to the test stage.
> * The env-build, snapshot-image-build, and leaked-resource-cleanup stages run in parallel; the test stage `needs:` both build stages.
> * Implement the currently-stubbed orchestrator commands (`_deploy_shared_env`/`_destroy_env`/`_sweep_stale_envs`) in `apps/minds/scripts/test_deployments.py` and drive them from CI.
> * Reconcile Modal auth in CI by having the job write a throwaway `~/.modal.toml` `minds-dev` profile from `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` so deploy-mode activation passes unmodified.
>
> The idea is that we can then have the snapshot *test* stage actually *use* this temporary minds CI environment that was created.
>
> * Make the test stage a general offload runner where tests select any combination of "uses the snapshot image" (`minds_snapshot_resume`) and/or "uses the per-run CI env" (`minds_services`) via composable capability markers; point `minds_services` at the per-run CI env.
> * Run one offload pass that boots every test-stage test from the snapshot image (`--override-image-id`) and injects the CI-env config, with separate `[groups.*]` per tier.
> * Support two tiers via the existing `release` marker: integration tier (capability-marked, not `release`) runs every push; release tier (incl. `release` + `minds_deployment`) runs only on a new `workflow_dispatch` job reusing the same env-build + offload + Vault machinery.
> * Pass the env name + non-secret URLs (connector_url, litellm_proxy_url) to the test stage via a GH job output / `deployment_envs.json` artifact; secrets come via `use-vault-secrets`.
> * Phase the work: Phase 1 = minimal working pipeline (env-build + cleanup stages, implemented orchestrator deploy/destroy/sweep, test stage depending on both builds, one new `minds_services` integration test); Phase 2 = the refactor (fold all `minds_services`/`minds_deployment` tests in, the release tier, marker/tier generalization, local-invocation polish).
> * Standardize local debugging on existing loops: stand up one reusable env once (`minds env deploy` / orchestrator `up`), run individual tests via `deployment_envs.json` + `services-against`; snapshot tests via `test-offload-minds-snapshot <image> --filter`.
>
> Because it ends up creating real live cloud resources, we'll need to ensure that they get cleaned up.
>
> This should be done in two ways:
>
> 1. *another* stage that runs at the *same* time as the (current) snapshot build and (new) env build stage, where this new stage is responsible for cleaning up outdated, leftover minds env resources that "leaked"
> 2. by convention, after the minds snapshot *test* stage (the second one) finishes the offload run, it should go ahead and destroy those test resources that were created for that minds CI env
>
> * Implement cleanup as the (now-real) age-based `ci-*` env sweep + ledger reconciliation in the parallel stage, with a 1-hour age threshold; make the per-run destroy an `if: always()` CI step after the offload step in the test job.
>
> (the reason for the first phase is in case the second one fails or hard crashes, as a backup to clean up anything left over)
>
> Note that creating CI environments might require some additional vault access (in which case we should add that)
>
> * No new Vault secret values are needed (`secrets/minds/ci/*` already holds cloudflare, litellm, neon, neon-admin, ovh, pool-ssh, supertokens); add a new OIDC role `minds_ci_env_gh` + GitHub Environment `minds-ci-env` (in the `imbue-ai/vault` repo, `jwt_role_and_policy` pattern) for the env-build and cleanup jobs.
> * Authenticate to Modal via the existing `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` GitHub var/secret, reused by the new jobs.
>
> As part of making this spec, we should make a single test that exercises it ... log in to a test imbue cloud account ... Once logged in, we should check that eg we can mint a litellm key or something.
>
> * Skip email entirely: the env-build step uses the SuperTokens admin API to create a verified test user with a fixed email + password; the test logs in with those.
> * Store fixed `CI_TEST_USER_EMAIL` + `CI_TEST_USER_PASSWORD` in Vault under `secrets/minds/ci/paid-accounts/`; both stages read them via `use-vault-secrets`.
> * Make the account paid by using an `@imbue.com` email — `imbue.com` is already seeded into `paid_domains` by the `ci` tier `deploy.toml [paid]`, so no admin-key mutation and no `[paid]` edit are required (validated live).
> * The new integration test mints a LiteLLM key and makes one real LLM call through the returned `base_url`, asserting a successful completion.
> * Solve dynamic per-env secret passing (the freshly-created SuperTokens app + Neon project secrets) in Phase 1, using one mechanism that works identically locally and in CI: env-build writes per-run dynamic secrets to a per-run Vault path (`secrets/minds/ci/runs/<run_id>/...`); both the local and CI test runner read them back and populate the existing `MINDS_DEPLOYMENT_TEST_SHARED_*` env vars; per-run destroy + the sweep delete the path.
> * `CI_TEST_USER_PASSWORD` is a static Vault secret required in Phase 1 by both the env-build job (to create the user) and the test job (to log in).
> * Delete the two never-run `@skip`'d `minds_services` tests (`test_litellm_via_workspace.py`, `test_signup_tunnel.py`) as part of this work.
> * Keep the now-unused FCT-branch-push + mail.tm scaffolding in place for Phase 2's workspace/signup tests.
> * Unify the offload version pin to `0.9.10` across the snapshot/new jobs.
> * Schedule the `imbue-ai/vault` + GitHub setup as the very first step (Phase 0), and provide the exact commands to deploy it.

---

## Validation performed (real infrastructure, before writing this plan)

Every load-bearing assumption was checked by running real commands against live infra:

- Vault reads of all `secrets/minds/ci/*` leaves we depend on (neon-admin, supertokens core, litellm master key, anthropic key) succeed.
- Vault per-run path write → read → delete at `secrets/minds/ci/runs/<probe>/...` works (the Phase 1 dynamic-secret mechanism).
- The full smoke chain works end-to-end against both an existing `dev` env and a **freshly deployed `ci` env**: SuperTokens admin signup → email-verify → connector `/auth/signin` → `require_paid_account` passes for an `@imbue.com` email → connector `/keys/create` mints a LiteLLM key → real `claude-haiku-4-5` completion via the proxy `base_url` (`/chat/completions`) → key + user deleted.
- A throwaway `ci-<ts>-<uuid>` env **deployed in ~2 minutes** (Modal env + Neon project w/ 2 DBs + SuperTokens app + 12 migrations + `imbue.com` paid-domain seed + 7 Modal secrets + `llm-ci`/`rsc-ci` apps + health checks) and **destroyed cleanly** (no leaked Modal env / Neon project / SuperTokens app / env dir / recover-target file).
- `@imbue.com` is paid out of the box via the `ci` `deploy.toml [paid] domains = ["imbue.com"]` seed — confirmed in the deploy log (`Seeding default paid-list entries (domains=['imbue.com'])`).
- `minds-dev` Modal profile exists locally and `minds env activate --deploy` pins it for the `ci` tier; `minds env deploy`/`destroy` for `ci` need no confirmation flags.
- offload (local `0.9.7`) supports `--override-image-id`, `--env`, `--collect-only`, `[groups.*]`.

Could not be validated locally (inherently CI-only; treated as residual risk, see Open considerations): the GitHub-OIDC Vault role + GitHub Environment binding, the CI throwaway `~/.modal.toml` profile, offload `0.9.10` flag parity, and the specific offload run that boots `minds_services` tests from `--override-image-id` with injected per-run CI-env config.

---

## Overview

- **Goal:** add a precursor CI stage that stands up one ephemeral per-run minds `ci-*` environment, let the snapshot test stage run live tests against it, and guarantee teardown of the real cloud resources it creates. The deferred GitHub-Actions integration of the existing `minds-deployment-tests` design becomes real.
- **Build on what exists:** the `ci` env tier (`config/envs/ci/deploy.toml`), the `minds env deploy/destroy` CLI, the `minds_services`/`minds_deployment` marks and their fixtures, the deployment-tests orchestrator with its ledger + run-id + (currently stubbed) deploy/destroy/sweep, and the existing two-stage snapshot pipeline (`build-minds-snapshot` → `test-minds-snapshot`) with Vault-OIDC and a periodic Modal cleanup job. The orchestrator stubs and the CI wiring are the real new work.
- **Two-tier test stage:** the test stage becomes a general offload runner. Tests select capabilities via composable marks — `minds_snapshot_resume` (needs the prebuilt snapshot image) and/or `minds_services` (needs the per-run CI env). The `release` mark is the tier switch: integration tier runs every push; release tier runs only on a manual `workflow_dispatch`.
- **Two-layer cleanup:** (1) a parallel cleanup stage runs the now-real age-based `ci-*` sweep + ledger reconciliation (1-hour threshold) as a backup for leaks; (2) by convention the test job destroys its own per-run env in an `if: always()` step after offload.
- **Least-privilege secrets, one mechanism for local + CI:** a new `minds_ci_env_gh` Vault OIDC role + `minds-ci-env` GitHub Environment grant the env-build/cleanup jobs the `secrets/minds/ci/*` access they need (no new secret values — the tree already has them). Dynamic per-env secrets (freshly created SuperTokens app + Neon project) flow env-build → test via a per-run Vault path that both the local and CI test runner read back into the existing `MINDS_DEPLOYMENT_TEST_SHARED_*` env vars.
- **Phased delivery:** Phase 0 = Vault/GitHub setup (manual handoff, exact commands provided). Phase 1 = minimal working pipeline + one new integration test, proven green. Phase 2 = fold all `minds_services`/`minds_deployment` tests in, add the release tier, generalize the marker/tier model, polish local invocation.

## Expected behavior

- **On every non-fork push/PR (behind the `DISABLE_MINDS_SNAPSHOT_CI` kill switch):**
  - Three jobs run in parallel: `build-minds-snapshot` (unchanged — builds the Modal snapshot image), a new `build-minds-ci-env` (deploys one ephemeral `ci-<timestamp>-<uuid>` env), and a new `cleanup-minds-ci-envs` (sweeps leaked `ci-*` envs older than 1 hour + reconciles the ledger).
  - `build-minds-ci-env` deploys the env, creates a verified fixed `@imbue.com` CI test user via the SuperTokens admin API, writes the per-run dynamic secrets to `secrets/minds/ci/runs/<run_id>/...` in Vault, emits the env name + non-secret URLs as a job output / `deployment_envs.json` artifact.
  - `test-minds-snapshot` `needs:` both build jobs. It boots the offload suite from the snapshot image (`--override-image-id`), injects the per-run CI-env config (URLs from the artifact; secrets read back from the per-run Vault path → `MINDS_DEPLOYMENT_TEST_SHARED_*`; `CI_TEST_USER_*` + `ANTHROPIC_API_KEY` from static Vault), and runs the **integration-tier** groups (`minds_snapshot_resume` and/or `minds_services`, excluding `release`).
  - After offload, an `if: always()` step destroys the per-run env and deletes its per-run Vault path — whether tests passed or failed.
  - A new `minds_services` integration test logs in as the fixed CI user via the connector, mints a LiteLLM key, and makes one real LLM call through the returned `base_url`, asserting success.
- **On manual `workflow_dispatch` (before a release):** the same machinery runs the **release-tier** groups instead (includes `release` + `minds_deployment`). `minds_deployment` tests mint their own ephemeral envs; the per-run shared env still backs `minds_services` tests.
- **If the per-run destroy step never runs (job hard-crash/cancel):** the next push's parallel `cleanup-minds-ci-envs` job reclaims the leaked env (and its Vault path) once it is older than 1 hour.
- **Locally:** a developer stands up one reusable env once (orchestrator `up` / `minds env deploy`), then runs any individual test against it via the same `deployment_envs.json` + per-run-secret mechanism (`minds-test-services-against` / the printed `pytest -m minds_services` command); snapshot tests run via `test-offload-minds-snapshot <image> --filter`. The local and CI paths share one code path for env config + secrets.
- **Unchanged:** non-CI `minds env` usage; the standard `test-offload`/`acceptance`/`release` jobs; fork PRs still skip the snapshot/env jobs (no OIDC token).

## Changes

### Phase 0 — Vault + GitHub setup (manual handoff; do first)

- In the `imbue-ai/vault` repo (`terraform/github_actions.tf`, `jwt_role_and_policy` pattern): add role `minds_ci_env_gh`, `bound_claims = { repository = "imbue-ai/mngr", environment = "minds-ci-env" }`, `user_claim = "iss"`, allowing read of the `secrets/minds/ci/*` paths an env deploy/destroy needs (cloudflare, litellm, neon, neon-admin, ovh, pool-ssh, supertokens, paid-accounts) plus read/write/delete under `secrets/minds/ci/runs/*` (per-run dynamic secrets).
- Expand `minds_ci_test_gh` to also read `secrets/minds/ci/paid-accounts/CI_TEST_USER_*` and `secrets/minds/ci/runs/*` (so the test job reads back per-run dynamic secrets + the static CI-user creds).
- Add Vault values: `secrets/minds/ci/paid-accounts/CI_TEST_USER_EMAIL` (an `@imbue.com` address) and `.../CI_TEST_USER_PASSWORD`.
- Create GitHub Environment `minds-ci-env` on `imbue-ai/mngr` (all same-repo branches, no required reviewers); confirm `minds-ci-test` exists.
- Deliverable: the exact `terraform apply` + GitHub Environment creation + `vault kv put` commands, documented as the Phase 0 runbook in the plan directory.

### Phase 1 — minimal working pipeline

- Implement the stubbed orchestrator commands in `apps/minds/scripts/test_deployments.py`: `_deploy_shared_env` (real `minds env activate --deploy --create` + `minds env deploy` for a `ci-*` env, parse `client.toml`), `_destroy_env` (real `minds env destroy`), `_sweep_stale_envs` (real age-based teardown of `ci-*` envs older than the threshold, default 1 hour). Wire the `run`/`up`/`down`/`cleanup` paths to the now-real functions.
- Add per-run dynamic-secret handling: after deploy, write the env's SuperTokens app + Neon DSN secrets to `secrets/minds/ci/runs/<run_id>/shared-<role>` in Vault; on the test side, read them back and populate `MINDS_DEPLOYMENT_TEST_SHARED_<ROLE>_<KEY>`; delete the path on destroy and in the sweep. One code path used by both local and CI runners.
- Create the fixed verified CI test user during env-build (SuperTokens admin signup + verify), using the `@imbue.com` email from Vault (paid via the already-seeded `imbue.com` domain).
- Add a new `minds_services` integration test (under `apps/minds/deployment_tests/`) that logs in as the fixed CI user via the connector, mints a LiteLLM key, and makes one real LLM call through `base_url`, asserting a successful completion.
- CI (`.github/workflows/ci.yml`): add `build-minds-ci-env` and `cleanup-minds-ci-envs` jobs (parallel to `build-minds-snapshot`), each using `environment: minds-ci-env` + `use-vault-secrets` (role `minds_ci_env_gh`) + the existing `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET`, and a step that synthesizes a throwaway `~/.modal.toml` `minds-dev` profile from those tokens. Make `test-minds-snapshot` `needs:` both build jobs, fetch the per-run env config + secrets, and add the `if: always()` per-run destroy step after offload. Unify offload pins to `0.9.10`.
- Offload config: extend `offload-modal-minds-snapshot.toml` (or a sibling) so the integration-tier group selects `minds_snapshot_resume` and/or `minds_services` (excluding `release`); forward the per-run CI-env env vars via `--env` in the `just` recipe. Remove the `and not minds_services` exclusion only where that marker now runs; keep the marker partition disjoint and every group non-empty.
- Delete the two never-run `@skip`'d tests (`apps/minds/deployment_tests/test_litellm_via_workspace.py`, `test_signup_tunnel.py`).
- Changelogs: `dev/` (CI + scripts), `apps/minds` (orchestrator, new test, offload/just wiring).

### Phase 2 — refactor (after Phase 1 is green)

- Generalize the capability + tier model: treat `minds_snapshot_resume` + `minds_services` as composable capability marks; use `release` as the integration-vs-release tier switch; document the matrix.
- Fold the remaining `minds_services` and `minds_deployment` tests into the test stage's offload groups (release-tier group includes `release` + `minds_deployment`); ensure each group stays non-empty.
- Add the release tier: a `workflow_dispatch`-triggered job (or input) reusing the env-build + offload + Vault machinery to run the release-tier groups before a release.
- Re-enable workspace/signup coverage using the retained FCT-branch-push + mail.tm scaffolding (implement `_push_fct_test_branch`/`_delete_fct_test_branch`); re-add workspace-creating tests.
- Polish local invocation: document the canonical debug loops (orchestrator `up`/`down`, `minds-test-services-against`, `test-offload-minds-snapshot --filter`) so any individual test is cheap to run against a stood-up env.

## Open considerations / residual risks

- **CI-only unknowns (cannot be validated locally):** the GitHub-OIDC `minds_ci_env_gh` role + `minds-ci-env` Environment binding; the throwaway `~/.modal.toml` profile written from token env; offload `0.9.10` flag parity (local is `0.9.7`; needed flags exist and are stable); and the first real offload run that boots `minds_services` tests from `--override-image-id` with injected per-run CI-env config. All are iterated to green in CI during Phase 1.
- **Cost/latency:** each push deploys + destroys a real env (~2 min deploy observed, cached). The 1-hour sweep bounds leak cost; consider gating to `main` + non-fork PRs if per-PR cost is a concern (currently every non-fork PR + push, behind the kill switch).
- **Benign destroy warning:** `minds env destroy` logs "could not resolve mngr profile user_id → skipping Docker state-container cleanup" when no workspace was created; harmless for the smoke-only case but worth confirming it stays benign once workspace tests are added in Phase 2.
- **Offload `--filter`:** not present in local `0.9.7`'s `run --help`; confirm single-test selection ergonomics in `0.9.10` (or rely on passing pytest node ids / `-k` through trailing args).
- **Per-run Vault path policy:** the `minds_ci_env_gh` role needs write+delete and `minds_ci_test_gh` needs read on `secrets/minds/ci/runs/*`; verify the KV v2 capabilities are set correctly in terraform (the path mechanics themselves are validated).
