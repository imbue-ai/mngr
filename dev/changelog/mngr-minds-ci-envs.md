CI: the minds snapshot test pipeline now stands up a real per-run minds `ci-*` environment that the test stage exercises with live tests, and tears it down afterward.

- New `build-minds-ci-env` job (parallel to `build-minds-snapshot`) deploys the per-run env via the deployment-tests orchestrator and publishes its per-run secrets to Vault.

- `test-minds-snapshot` now depends on both build jobs and additionally runs the `minds_services` tests (login + mint LiteLLM key + live LLM call) on the runner against the per-run env.

- New `destroy-minds-ci-env` job (`always()`) tears the per-run env down after the test stage; a new parallel `cleanup-minds-ci-envs` job sweeps leaked `ci-*` envs older than 1 hour as a backstop.

- New Vault-OIDC auth in these jobs uses the `minds_ci_env_gh` / `minds_ci_test_gh` roles (env `minds-ci-env` / `minds-ci-test`); the ci-env jobs deploy to the minds-dev Modal workspace via `MINDS_DEV_MODAL_TOKEN_*`; the snapshot-test offload pin is unified to `0.9.10`.

- New `workflow_dispatch` input `run_minds_release_tests` + `test-minds-release` job: the manual release tier that runs the heavy `minds_deployment` tests (deploy / rollback / round-trip), each minting + destroying its own ephemeral env.

- Standing up a per-run `ci-*` env is now opt-in: `build-minds-ci-env`, `cleanup-minds-ci-envs`, `destroy-minds-ci-env`, and the `minds_services` step in `test-minds-snapshot` run ONLY on a `workflow_dispatch` with `run_minds_release_tests=true` (the same switch that gates `test-minds-release`). Normal pushes/PRs no longer create any ci env -- `test-minds-snapshot` still runs the `minds_snapshot_resume` tests (which need only the built snapshot image), via `always()` + a `build-minds-snapshot` success gate so the opt-in (skipped) ci-env build does not skip the whole job.
