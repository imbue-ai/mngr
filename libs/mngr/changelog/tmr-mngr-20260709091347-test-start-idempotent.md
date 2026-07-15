Fixed the `test_start_idempotent` e2e tutorial test (STARTING AND STOPPING AGENTS section): removed a superfluous `@pytest.mark.rsync` mark. Creating a local agent from a clean git repo uses a git worktree and never invokes rsync, so the resource guard failed the otherwise-passing test for carrying a mark it did not use.

Strengthened the same test to confirm the agent is already running before the idempotent `mngr start`, so it genuinely verifies idempotence on a running agent rather than the stopped-agent path.
