# minds deployment + services tests

End-to-end tests that exercise the real deployed minds services and the deploy process itself. See [`specs/minds-deployment-tests.md`](../../../specs/minds-deployment-tests.md) for the full design.

## Marks (capability) and tiers

Each test carries one capability mark describing the infrastructure it needs:

- `pytest.mark.minds_services` -- runs against a pre-stood-up shared ci env (connector + litellm + SuperTokens). Fast; no env minting.
- `pytest.mark.minds_deployment` -- mints its own ephemeral `ci-*` env via `minds env deploy` and tears it down (slow, real cloud spend). Exercises the deploy/rollback/destroy process itself.

(The snapshot-resume suite under `apps/minds/test_snapshot_resume.py` carries a separate `minds_snapshot_resume` capability mark; a test may compose marks when it needs more than one capability.)

These map onto two **tiers**:

| Tier | When it runs | What runs |
|---|---|---|
| Integration | every push / PR (non-fork), in the `test-minds-snapshot` CI job | `minds_services` (on the runner, against the per-run env from `build-minds-ci-env`) + `minds_snapshot_resume` (offload) |
| Release | manual only, in the `test-minds-release` CI job | `minds_deployment` (each test mints + destroys its own env) |

The release tier is `workflow_dispatch`-gated because each `minds_deployment` test deploys a full env (minutes + real spend). Trigger it from the Actions UI or:

```bash
gh workflow run ci.yml -f run_minds_release_tests=true --ref <branch>
```

Both marks are excluded from the standard `test-offload` jobs and from `just test-quick`; they run only via the CI jobs above or the `just minds-test-*` recipes below.

## Running locally

```bash
# Release tier (minds_deployment): each test mints + destroys its own ephemeral env.
# Needs `vault login` + a minds-dev Modal profile.
just minds-test-deployment-only
# ...or a single one:
just minds-test-deployment-only apps/minds/deployment_tests/test_deploy_round_trip.py

# Integration tier (minds_services) against a reusable shared env you stand up once:
just minds-test-deployment-up default
# ...copy/run the printed `MINDS_DEPLOYMENT_TEST_ENVS_JSON=... pytest -m minds_services` command...
just minds-test-deployment-down

# Or point the services tests at an already-deployed dev env (no env create/destroy):
just minds-test-services-against dev-josh apps/minds/deployment_tests/test_logged_in_smoke.py

# Clean up anything left over from a prior aborted run:
just minds-test-deployment-cleanup
```

The `shared_env` / `ci_test_user` fixtures resolve their secrets from injected env vars when present (the CI path) and otherwise from Vault (local runs, where you have a token), so the same test body runs in both places.

## Prerequisites

- `vault login` so `minds env deploy` and the fixtures can read tier secrets.
- A minds-dev Modal profile (`~/.modal.toml [minds-dev]`) for the deploy/destroy steps.
- For the (currently skipped) workspace/signup tests only: a `git worktree` of `forever-claude-template` at `<monorepo>/.external_worktrees/forever-claude-template/` and a running Docker daemon. A missing FCT worktree is now a warning (no current test needs it), not a hard failure.

## Status

- `test_logged_in_smoke` (`minds_services`) and `test_ci_env_litellm` (`minds_services`: login → mint LiteLLM key → live LLM call) run in the integration tier and pass in CI.
- `test_deploy_new_version` and `test_deploy_round_trip` (`minds_deployment`) run in the release tier and pass.
- `test_deploy_rollback` (`minds_deployment`) is **`@pytest.mark.skip`ped**: it surfaced a real `minds env recover` gap (after auto-rollback the broken v2 containers are not terminated, so `/version` still reports the failed deploy_id). It needs a fix in the recover/terminate path before it can pass; see its skip note.
- `test_litellm_via_workspace` and `test_signup_tunnel` are wired into the flow but **`@pytest.mark.skip`ped**: their bodies are still stubs and need debugging/implementation (real FCT Docker workspace creation, Cloudflare tunnels, the mail.tm signup flow) before they will pass. Each carries an explicit skip note.
