The minds release-tier tests (`deployment_tests/`, marked `minds_deployment` / `minds_services`) now also carry `@pytest.mark.release`, so the whole release suite -- mngr and minds -- is discoverable by the `release` tag rather than by path.

These minds tests still run only from the minds jobs in `ci.yml` (manual `run_minds_release_tests` dispatch), because they need a remote ci env. The mngr release workflow selects `release` but excludes the minds capability marks (`minds_deployment` / `minds_services` / `minds_snapshot_resume`), so it never tries to stand one up. The plain minds `@release` tests that need no ci env (`test_claude_version_alignment`, `test_sse_redirect`, `test_aws_workspace_release`) continue to run in the mngr release workflow.

Docs updated: `deployment_tests/README.md` and `docs/testing-overview.md`.
