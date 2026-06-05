Fixed the `test_troubleshoot_check_agent_state` e2e tutorial test: added the
`@pytest.mark.timeout(120)` override that every other create-based e2e tutorial
test uses (the global 10s function-body timeout was killing the create + list)
and removed the spurious `@pytest.mark.modal` mark, since the test only creates a
local command agent and never exercises Modal. Also strengthened the assertions to
verify the just-created agent actually appears in `mngr list` output with its
resolved `local` provider, rather than only checking the command's exit code.
