Fixed the `test_command_agent_dev_server_extra_windows` e2e release test, which
was marked `@pytest.mark.modal` but never ran any command that contacts Modal, so
the resource guard failed it. It now runs `mngr list` (which exercises the Modal
discovery path, matching the sibling create tests) to verify the command agent was
created, and asserts that the extra `logs` tmux window requested via `-w` actually
exists.
