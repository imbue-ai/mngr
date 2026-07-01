The minds release-tier tests (`deployment_tests/`, marked `minds_deployment` / `minds_services`) now also carry `@pytest.mark.release`, so the whole release suite -- mngr and minds -- is discoverable by the `release` tag rather than by path.

All minds release tests now run from the minds release job (`test-minds-release` in `ci.yml`, manual `run_minds_release_tests` dispatch), never from the mngr release workflow. That job now runs the heavy `minds_deployment` group (via the deployment orchestrator) and then the plain minds `@release` tests that need no ci env -- `test_claude_version_alignment`, `test_sse_redirect` (Chromium installed in-job), and `test_aws_workspace_release` (skips without AWS opt-in) -- selected by tag. Previously those three ran in the mngr release workflow on `v*` tags; they now run on the minds release dispatch instead, matching the minds release procedure.

Fixed two pre-existing failures in the plain minds release tests that folding them into the minds release job surfaced:

- `test_sse_redirect` was stale: it drove `/creating/<agent-id>`, but the creating page now takes a `CreationId` and polls the v1 operations resource (`/api/v1/workspaces/operations/create/<creation_id>`) for completion. Reworked it to key the fake creation by a `CreationId`, mount the `/api/v1` blueprint (pass `paths`), and assert the canonical `/goto/<agent>/` redirect.

- `test_claude_version_alignment` was failing on a real drift (the release Dockerfile pinned an older Claude Code version than forever-claude-template); fixed by bumping the pin (see the mngr changelog).

Docs updated: `deployment_tests/README.md` and `docs/testing-overview.md`.
