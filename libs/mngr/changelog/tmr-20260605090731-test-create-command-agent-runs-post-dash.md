Removed the inapplicable `@pytest.mark.modal` mark from the release e2e test
`test_create_command_agent_runs_post_dash_command_in_agent`. The test creates a
`command`-type agent on the default (local) provider and never invokes Modal in
any way the resource guard can observe, so the mark caused a spurious
NEVER_INVOKED guard failure. The test still exercises the documented `mngr create
--type command -- <command>` behavior and verifies the command actually runs in
the agent.
