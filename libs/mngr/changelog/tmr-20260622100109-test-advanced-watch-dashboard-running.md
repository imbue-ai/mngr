Fixed the `test_advanced_watch_dashboard_running` e2e release test (covering the `watch -n 5 mngr list --running` dashboard tutorial block):

- Added an explicit `@pytest.mark.timeout(120)` so the test no longer inherits the 10s global pytest timeout and fails while a fresh `mngr list` invocation is still running.

- Scoped the verification query to `--provider local`. `mngr list` defaults to `--on-error abort`, so an unscoped `mngr list --running` aborts when a credential-less cloud backend (e.g. the always-registered `aws` plugin in this monorepo) is queried. Scoping keeps the dashboard assertion deterministic regardless of which provider plugins happen to be installed.

- Added a companion test `test_advanced_watch_dashboard_running_filters_idle_agent` that creates a live-but-idle local agent and verifies the `--running` filter genuinely reflects agent state: the agent appears (as `WAITING`) in the unfiltered dashboard but is excluded by `mngr list --running`.
