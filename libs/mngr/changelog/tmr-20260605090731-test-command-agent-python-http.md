Fixed and strengthened the `test_command_agent_python_http` e2e tutorial test
for the "RUNNING NON-AGENT PROCESSES" section. The test created a local
`--type command` agent but was incorrectly marked `@pytest.mark.modal`, so the
resource guard failed it for never invoking Modal; the mark was removed. The
test now also verifies actual behavior: it checks that the managed process is
running inside the agent (`mngr exec ... ps`) and that the agent is listed as a
local command agent with the expected command (`mngr list --provider local
--format json`). No production code changes.
