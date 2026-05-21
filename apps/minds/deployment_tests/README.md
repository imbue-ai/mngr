# minds deployment + services tests

End-to-end tests that exercise the real deployed minds services and the deploy process itself. See [`specs/minds-deployment-tests.md`](../../../specs/minds-deployment-tests.md) for the full design.

## Marks

Every test in this directory carries one of:

- `pytest.mark.minds_deployment` -- mints its own ephemeral ci env via `minds env deploy` (slow, costs real cloud resources).
- `pytest.mark.minds_services` -- runs against a pre-stood-up shared ci env (fast).

Both marks are explicitly excluded from the standard CI offload jobs and from `just test-quick`, so the tests only run when invoked via `just minds-test-deployment` (or one of its sibling recipes).

## Running

```bash
# Full suite (deploys shared envs, runs both batches sequentially, tears everything down):
just minds-test-deployment

# Cleanup anything left over from a prior aborted run:
just minds-test-deployment-cleanup

# Local iterate: stand up a shared env, get a pytest-ready command, tear down on demand:
just minds-test-deployment-up default
# ...copy/run the printed pytest command...
just minds-test-deployment-down

# Run the services tests against your own already-deployed dev env (no env create/destroy):
just minds-test-services-against dev-josh apps/minds/deployment_tests/test_logged_in_smoke.py
```

## Prerequisites

- Operator has run `vault login` so `minds env deploy` can read tier secrets.
- A `git worktree` of `forever-claude-template` exists at `<monorepo>/.external_worktrees/forever-claude-template/`, matching whichever FCT branch you're testing against. The orchestrator errors out at startup with the setup command if missing.
- A Docker daemon is running for the litellm-via-workspace test.

## Status

All tests are currently `@pytest.mark.skip`ped while the orchestrator + fixture implementations land. The test bodies are skeleton-quality and document the planned assertions; iterating on them is the next step after the scaffolding ships.
