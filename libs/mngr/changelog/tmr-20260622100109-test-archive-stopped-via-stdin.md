Fixed the `test_archive_stopped_via_stdin` tutorial e2e test (STARTING AND STOPPING AGENTS section):

- Added `@pytest.mark.timeout(180)` so the test no longer hits the default 10s per-test timeout while creating and stopping a local agent (matching its sibling lifecycle tests).

- Scoped the test's own precondition/effect verification commands to the local provider (`mngr list ... --provider local`), since the agent under test runs locally. This keeps the assertions independent of whichever remote backends (docker, the cloud providers, ...) happen to be installed and enabled in the workspace; those are unconfigured/unreachable in the isolated e2e environment and would otherwise make a bare `mngr list` exit non-zero. The tutorial command itself (`mngr list --stopped --ids | mngr archive -`) is left exactly as written.

- Removed the spurious `@pytest.mark.rsync`: a `--type command` local agent running `sleep` creates its worktree via git and never invokes rsync, so the resource guard correctly rejected the mark.
