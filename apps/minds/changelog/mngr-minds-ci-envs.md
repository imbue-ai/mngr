Added a per-run minds CI environment to the snapshot test pipeline so live tests can run against a real, isolated cloud env that is stood up before the run and destroyed after, plus a manual release tier for the heavier deploy tests.

- The deployment-tests orchestrator (`apps/minds/scripts/test_deployments.py`) now actually deploys a `ci-*` env (`_deploy_shared_env`), creates a fixed verified CI test user against it, publishes the env's freshly-minted per-env secrets to Vault, destroys envs by name (`_destroy_env`, reconstructing `secrets.toml` from Vault when run on a different machine), and sweeps leaked `ci-*` Modal envs by age (`_sweep_stale_envs` + a new `sweep` command). These were previously stubs.

- Per-env dynamic secrets (the env's own SuperTokens app + Neon DSNs) are handed between jobs via an env-name-keyed Vault path (`secrets/minds/ci/runs/<env-name>/shared-<role>`); the `shared_env` fixture resolves them from injected env vars or Vault, and a new `ci_test_user` fixture supplies the fixed CI credentials.

- New `minds_services` integration test: log in to the per-run env as the fixed CI user, mint a LiteLLM key, and make a live LLM call. Runs on every push.

- Test tiers: `minds_services` runs in the integration tier (every push); `minds_deployment` (deploy / rollback / round-trip) runs in a manual release tier (`workflow_dispatch`), all three passing. See `apps/minds/deployment_tests/README.md` for the capability-mark + tier matrix and local-invocation recipes.

- Fixed a `minds env recover` bug surfaced by the rollback test: after an auto-rollback, the rolled-back app's broken containers were never terminated because the Modal app-id lookup matched the app name against `Name`/`name`/`App` but `modal app list --json` reports it under `Description`. The lookup silently found nothing, so the failed version's containers kept serving (and `/version` kept reporting the failed deploy_id) until they idled out. `_find_modal_app_id` now also checks `Description`; the parsing is extracted into a pure `_select_deployed_app_id` with regression unit tests against the real JSON shape.

- `test_litellm_via_workspace` and `test_signup_tunnel` are wired into the flow but remain `@pytest.mark.skip`ped with explicit notes: their bodies are still stubs and need debugging (real FCT Docker workspace creation, Cloudflare tunnels, the mail.tm signup flow) before they will pass.
