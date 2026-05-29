Fixed the `test_create_with_json_output` e2e tutorial test: it now carries an
explicit `@pytest.mark.timeout(120)` (the global 10s timeout was too short for an
agent-create) and no longer carries the superfluous `@pytest.mark.modal` mark
(the command uses the local provider, so modal is never invoked).

Strengthened the test to verify the `--format json` output of `mngr create`
itself (parsing the emitted `agent_id`/`host_id` JSON), and added a companion
`test_create_with_quiet_output` covering the `--quiet` line of the same tutorial
block.
