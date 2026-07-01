The minds release-tier tests (`deployment_tests/`, marked `minds_deployment` / `minds_services`) now also carry `@pytest.mark.release`, so the whole release suite -- mngr and minds -- is discoverable by the `release` tag rather than by path.

All minds release tests now run from the minds release job (`test-minds-release` in `ci.yml`, manual `run_minds_release_tests` dispatch), never from the mngr release workflow. That job now runs the heavy `minds_deployment` group (via the deployment orchestrator) and then the plain minds `@release` tests that need no ci env -- `test_claude_version_alignment`, `test_sse_redirect` (Chromium installed in-job), and `test_aws_workspace_release` (skips without AWS opt-in) -- selected by tag. Previously those three ran in the mngr release workflow on `v*` tags; they now run on the minds release dispatch instead, matching the minds release procedure.

Docs updated: `deployment_tests/README.md` and `docs/testing-overview.md`.
