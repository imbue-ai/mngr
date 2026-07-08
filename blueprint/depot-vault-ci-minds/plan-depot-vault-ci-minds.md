# Plan: Vault-backed secrets in CI + depot.dev builds for the minds snapshot pipeline

## Refined prompt

> I added the depot token in the correct locatoin. Now let's work through the actual work that we want to do: we want to build on the work that was done in the josh/explore-tests branch, where we got tests working remotely for minds by making two new github CI stages (one to build the modal image that includes the docker image and workspace, and then a later one to use that modal image for testing).
>
> What we want to do now is update that modal image building step to use depot.dev for the building (rather than building directly on modal). This will significantly improve the build speed, since the depot.dev will cache the earlier layers.
> * Build only the inner FCT agent container (built by dockerd inside the sandbox during workspace creation) via depot — not the outer Modal sandbox image, whose caching already works
> * Reuse mngr's existing docker-provider depot builder (`builder=DEPOT` → `depot build --load`) with no registry push, rather than authoring a new build path
> * Keep forever-claude-template's existing Dockerfile and build context unchanged; no new Dockerfile is written
> * Pre-install the depot CLI into the outer Modal snapshot image (CI-only, Modal-cached); not in the FCT Dockerfile or post-boot
> * Enable depot via env override (`MNGR__PROVIDERS__DOCKER__BUILDER=DEPOT`) scoped to the sandbox
> * Supply `DEPOT_PROJECT_ID=fsjzltqvxq` as a non-secret env constant
> * Hard-fail the build job if `DEPOT_TOKEN` is missing or `depot build` fails (no silent fallback)
> * Depot is enabled solely by the CI-set override; local/non-CI runs default to DOCKER and need no token; a missing token hard-fails only when the override is set
> * Verify depot was used via a lightweight build-log assertion
>
> We'll also want to include various secrets (ex: ANTHROPIC_API_KEY) in to the second CI stage, so that we can do various (live) tests and ensure that everything is actually working.
> * Expose `ANTHROPIC_API_KEY` to the test stage from `minds/ci/litellm/ANTHROPIC_API_KEY`; just make it available to the existing suite, no new live assertions yet
> * Stage 2 also receives `DEPOT_TOKEN` + `builder=DEPOT` so a fresh-workspace rebuild hits depot's remote cache; add a new stage-2 test that creates a fresh workspace to prove the depot rebuild path works and is cached
> * The new stage-2 test asserts a depot **cache hit** (not just that depot ran), via parsing `CACHED` markers in the build output (deterministic, not timing-based); it builds/creates only (no live Claude turn); added to `apps/minds/test_snapshot_resume.py` under the existing `minds_snapshot_resume` marker; not marked flaky up front
>
> In order to do this, we'll need to do some of the vault stuff that you mentioned above (eg, to get vault working for the mngr repo and exposing the correct secrets from the minds/ci path -- ideally we can easily control which of them are accessible from which github CI flows
> * Per-CI-job vault roles bound to `imbue-ai/mngr`, each gated on its own GitHub Environment claim
> * Two GitHub Environments — one per job (`minds-ci-build`, `minds-ci-test`) — so each stage only has the secrets it needs and nothing leaks into the snapshot image
> * Environments allow all same-repo branches with no required reviewers, so both jobs run unattended on every PR
> * Tightest secret scoping: build role (`minds_ci_build_gh`) reads only `minds/ci/depot/DEPOT_TOKEN`; test role (`minds_ci_test_gh`) reads `minds/ci/depot/DEPOT_TOKEN` + `minds/ci/litellm/ANTHROPIC_API_KEY`
> * Inline the `export-secrets` composite action in both jobs for now; generalize later
> * Jobs run on same-repo branches + main only; fork PRs (no OIDC `id-token`) don't run them
>
> Basically -- we want to integrate vault secrets into our CI pipelines, and as a first real use case, get the docker images for the modal image build stage to be built in depot.dev so that it can re-use cached layers and be faster (and as a second use case, we want the anthropic api key exposed to the next stage so that we can do a real check that things are working)
> * Changes land in `dev/` (CI + snapshot script), `apps/minds`, and the `imbue-ai/vault` repo (terraform role; the `DEPOT_TOKEN` value is already added)
>
> Please work through any questions using your skill
>
> Use the concise template

---

## Overview

- **Goal:** make the minds snapshot CI faster by building the inner forever-claude-template (FCT) agent container via depot.dev's remote builder (shared layer cache) instead of a cold local `docker build`, and stand up Vault-backed secrets in GitHub CI as the supporting mechanism.
- **Key reuse:** mngr's docker provider already supports `builder=DEPOT` (`depot build --load`). We enable it via a CI-only env override (`MNGR__PROVIDERS__DOCKER__BUILDER=DEPOT`) rather than new build code or changes to FCT. The only mngr gap is that the docker provider does not auto-install the depot CLI, so we bake the CLI into the outer Modal snapshot image (Modal-cached, no per-run latency).
- **Secrets via Vault OIDC:** both CI jobs authenticate to Vault with GitHub OIDC through the existing `imbue-ai/vault` `export-secrets` composite action. New per-job Vault roles, each gated on its own GitHub Environment, give least-privilege access: the build job gets `DEPOT_TOKEN`; the test job gets `DEPOT_TOKEN` + `ANTHROPIC_API_KEY`.
- **Security posture:** two GitHub Environments (`minds-ci-build`, `minds-ci-test`) keep each stage's secrets isolated and out of the snapshot image. Stage 1 builds the image with no Anthropic key; stage 2 supplies the live Anthropic key only at test time. Secrets are passed to the depot CLI as process env, never as build args, so nothing is baked into the FCT image.
- **Proof it works:** a new stage-2 test creates a fresh FCT workspace inside the resumed snapshot and asserts a depot **cache hit** (parsing `CACHED` build markers), confirming both that the depot rebuild path works and that the cache populated in stage 1 is reused.
- **Two-phase delivery:** Phase 1 is a small, self-contained set of changes (Vault terraform role definitions + the GitHub Environment/role config the user must `terraform apply` and create in GitHub). Phase 2 wires depot + Vault into the CI jobs and snapshot script, adds the verification test, and iterates to green end-to-end.

## Expected behavior

- **Build stage (`build-minds-snapshot`):**
  - Authenticates to Vault via OIDC using role `minds_ci_build_gh`, gated on the `minds-ci-build` GitHub Environment; injects `DEPOT_TOKEN` into the job env.
  - Runs the snapshot script, which forwards `DEPOT_TOKEN`, `DEPOT_PROJECT_ID=fsjzltqvxq`, and `MNGR__PROVIDERS__DOCKER__BUILDER=DEPOT` into the Modal `vm_runtime` sandbox.
  - Inside the sandbox, the depot CLI (pre-baked into the outer image) is on PATH; the FCT container is built with `depot build --load`, hitting depot's remote layer cache. First-ever run is cold (populates cache); subsequent runs are fast on the stable toolchain/deps layers.
  - Hard-fails if `DEPOT_TOKEN` is missing or `depot build` fails — no silent fallback to local `docker build`.
  - A lightweight assertion confirms the build actually went through depot (CLI invoked in logs).
- **Test stage (`test-minds-snapshot`):**
  - Authenticates to Vault via OIDC using role `minds_ci_test_gh`, gated on the `minds-ci-test` GitHub Environment; injects `DEPOT_TOKEN` + `ANTHROPIC_API_KEY`.
  - Boots from the stage-1 snapshot via offload `--override-image-id` and runs the `minds_snapshot_resume` suite; `ANTHROPIC_API_KEY` is available to the suite (no new live assertions on existing tests).
  - A new test creates a fresh FCT workspace (depot builder + token forwarded into the offload sandbox) and asserts a depot cache hit via `CACHED` markers; it builds/creates only (no live Claude turn).
- **Local / non-CI behavior unchanged:** without the env override, the snapshot script and all minds workspace creation default to `DOCKER` builder and require no depot token. Fork PRs (which GitHub denies `id-token`) do not run these jobs.
- **Isolation guarantees:** the depot token never reaches the snapshot image as a build arg; the Anthropic key is absent from stage 1, so it cannot be baked into the snapshot.

## Changes — Phase 1: Vault + GitHub setup (short; unblocks the user's deploy commands)

This phase produces everything the user needs to run the Vault/GitHub setup, and nothing that depends on it. After this phase the user runs `terraform apply` (in the `imbue-ai/vault` repo) and creates the two GitHub Environments; Phase 2 then has working auth to build against.

- **Vault terraform (`imbue-ai/vault` repo, `terraform/github_actions.tf`):**
  - Add role `minds_ci_build_gh`: backend = github_actions; `bound_claims = { repository = "imbue-ai/mngr", environment = "minds-ci-build" }`; `user_claim = "iss"`; `secrets = ["minds/ci/depot/DEPOT_TOKEN"]`.
  - Add role `minds_ci_test_gh`: same backend; `bound_claims = { repository = "imbue-ai/mngr", environment = "minds-ci-test" }`; `user_claim = "iss"`; `secrets = ["minds/ci/depot/DEPOT_TOKEN", "minds/ci/litellm/ANTHROPIC_API_KEY"]`.
  - Follow the existing `jwt_role_and_policy` module pattern (mirrors `vault_repo_test_gh` / the sculptor roles); keep roles in alphabetical order.
- **Vault secret values:** `minds/ci/depot/DEPOT_TOKEN` is already added by the user. No `DEPOT_PROJECT_ID` stored in Vault (it is a non-secret constant supplied via env). Confirm `minds/ci/litellm/ANTHROPIC_API_KEY` is populated (it exists today).
- **GitHub Environments (on `imbue-ai/mngr`):** create `minds-ci-build` and `minds-ci-test`; deployment branches = all (same-repo), no required reviewers, so PRs run unattended.
- **Changelog:** `imbue-ai/vault` repo per-PR changelog entry describing the two new roles.
- **User-run deploy commands (documented as the Phase 1 handoff):** `terraform init && terraform apply` in the vault repo; create the two GitHub Environments. These are the commands Phase 1 exists to unblock.

## Changes — Phase 2: depot + Vault wiring, verification test, end-to-end iteration (longer)

- **Outer snapshot image (`scripts/snapshot_minds_e2e_state.py`):**
  - Bake the depot CLI into the Modal image build (`curl -fsSL https://depot.dev/install-cli.sh | sh`), alongside the existing uv/claude installs, so it is Modal-cached and on PATH at sandbox boot.
  - Forward depot configuration into the sandbox env when present: `DEPOT_TOKEN`, `DEPOT_PROJECT_ID` (default the constant `fsjzltqvxq`), and `MNGR__PROVIDERS__DOCKER__BUILDER=DEPOT`. When `DEPOT_TOKEN` is absent (local runs), leave the builder at its default so the script still works without depot.
  - Ensure the depot env reaches the in-sandbox `mngr create` subprocess that builds the FCT container (sandbox env → Electron child → `mngr create`).
- **CI workflow (`.github/workflows/ci.yml`):**
  - `build-minds-snapshot`: add `environment: minds-ci-build` and `permissions: id-token: write`; add an `export-secrets` step (role `minds_ci_build_gh`) to inject `DEPOT_TOKEN`; pass depot env through to the snapshot script; add the lightweight "depot was used" log assertion; hard-fail on depot/token errors. Restrict triggers so the job runs on same-repo branches + main only (not fork PRs).
  - `test-minds-snapshot`: add `environment: minds-ci-test` and `permissions: id-token: write`; add an `export-secrets` step (role `minds_ci_test_gh`) to inject `DEPOT_TOKEN` + `ANTHROPIC_API_KEY`; forward `DEPOT_TOKEN` + `DEPOT_PROJECT_ID` + the builder override into the offload test sandbox so an in-test rebuild uses depot. Keep `DISABLE_MINDS_SNAPSHOT_CI` kill switch behavior.
  - Inline the `export-secrets` action usage in both jobs (no shared wrapper yet).
- **New verification test (`apps/minds/test_snapshot_resume.py`, `minds_snapshot_resume` marker):**
  - Inside the resumed snapshot sandbox, create a fresh FCT workspace via the depot builder, build/create only (no live Claude turn).
  - Assert a depot **cache hit** by parsing `CACHED` markers on the heavy layers in the build output (deterministic, not timing-based).
  - Not marked `@pytest.mark.flaky` initially; rely on offload's normal handling. (Offload handles parallelization.)
- **Offload env pass-through (open implementation detail):** confirm and use offload's mechanism for injecting `DEPOT_TOKEN`/`DEPOT_PROJECT_ID`/the builder override into the test sandbox; this becomes a concrete step once the mechanism is confirmed.
- **Changelogs:** `dev/` (CI + snapshot script) and `apps/minds` (new test) per-PR entries.
- **End-to-end iteration:** run the build job to populate depot's cache, confirm a second run hits the cache, confirm the test stage authenticates to Vault and the new test observes `CACHED`. Iterate until green in CI.

## Open items / risks (to resolve during Phase 2)

- **Offload env/secret pass-through:** exact mechanism for getting depot env into the offload test sandbox is unconfirmed; may require an offload flag or env-forwarding step.
- **Cross-repo dependency:** Phase 2 CI cannot pass until Phase 1's `terraform apply` + GitHub Environments exist (and the OIDC trust propagates).
- **Docker-provider depot CLI gap:** we intentionally work around it (bake CLI into the outer image) rather than fixing mngr core; if other docker-provider depot users appear, bringing the provider to parity (auto-install + `ensure_depot_token_available`) is a future improvement.
- **Cache-hit determinism:** the stage-2 `CACHED` assertion assumes the fresh build uses the same FCT context that stage 1 built; if the context differs, layers legitimately miss. The test must build the baked FCT context, not a re-cloned/divergent one.
