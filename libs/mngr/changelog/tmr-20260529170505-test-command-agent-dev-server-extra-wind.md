Fixed the `test_command_agent_dev_server_extra_windows` e2e tutorial test, which
was failing because it carried a superfluous `@pytest.mark.modal` mark even though
it only creates a local command agent. Also strengthened the test to verify the
extra `logs` tmux window is actually created alongside the agent's main window.
