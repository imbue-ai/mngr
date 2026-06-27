Added a per-run minds CI environment to the snapshot test pipeline so live tests can run against a real, isolated cloud env that is stood up before the run and destroyed after.

- The deployment-tests orchestrator (`apps/minds/scripts/test_deployments.py`) now actually deploys a `ci-*` env (`_deploy_shared_env`), creates a fixed verified CI test user against it, publishes the env's freshly-minted per-env secrets to a per-run Vault path, destroys envs by name (`_destroy_env`), and sweeps leaked `ci-*` Modal envs by age (`_sweep_stale_envs` + a new `sweep` command). These were previously stubs.

- Per-run dynamic secrets (the env's own SuperTokens app + Neon DSNs) are handed from env-build to the test runner via `secrets/minds/ci/runs/<run-key>/shared-<role>` in Vault; the `shared_env` fixture resolves them from injected env vars or Vault. A new `ci_test_user` fixture supplies the fixed CI credentials.

- New `minds_services` test: log in to the per-run env as the fixed CI user, mint a LiteLLM key, and make a live LLM call.

- Removed the two never-run `@skip`'d `minds_services` tests (`test_litellm_via_workspace`, `test_signup_tunnel`); the FCT-worktree requirement is now a warning rather than a hard failure (no current test needs it).
