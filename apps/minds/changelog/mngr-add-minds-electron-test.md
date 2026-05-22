Adds `test_create_local_docker_workspace_via_electron`: an acceptance test that drives the real Electron minds app via Playwright over CDP, clicks through the create form, waits for the workspace's `system_interface` dockview UI to render through the desktop client proxy, and cleans up the resulting `mngr` agent. Resolves the forever-claude-template source in three steps -- a local `.external_worktrees/` worktree first, then a shallow clone of the matching mngr branch on the FCT public remote, then `main` -- so the test runs unchanged in CI and against an operator's local FCT working tree.

Adds the `MINDS_MNGR_FORWARD_PORT` env var to `minds run` so test harnesses (and concurrent `just minds-start` invocations) can dodge the hardcoded default port 8421 collision.

Replaces the stale skipped `test_create_agent_e2e` (which never drove Electron and carried an out-of-date "TUI send-enter timeout" skip reason that no longer applies after FCT split its services agent from its chat agent).
