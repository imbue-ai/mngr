Fixed the `test_exec_branch_show_current` e2e tutorial test (WORKING WITH GIT
section). It was hitting the default 10s pytest timeout during agent creation and
carried a superfluous `@pytest.mark.modal` mark even though it only exercises a
local command agent. Added `@pytest.mark.timeout(60)` and removed the modal mark,
and strengthened the assertion to verify the agent reports its own
`mngr/{agent_name}` branch.
