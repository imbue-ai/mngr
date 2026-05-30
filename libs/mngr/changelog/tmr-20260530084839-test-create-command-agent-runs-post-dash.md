Removed the spurious `@pytest.mark.modal` from the e2e release test
`test_create_command_agent_runs_post_dash_command_in_agent`. The test creates a
local-provider command agent and never invokes Modal, so the resource guard
flipped the otherwise-passing test to failed via its `NEVER_INVOKED` check. The
remaining `@pytest.mark.release/tmux/rsync` marks are accurate.
