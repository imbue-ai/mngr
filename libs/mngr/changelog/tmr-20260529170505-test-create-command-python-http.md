Fixed the `test_create_command_python_http` e2e tutorial test: removed the
spurious `@pytest.mark.modal` (the test creates a local-provider `command`
agent and never exercises Modal), and added a verification step that the
created agent actually appears in `mngr list` as a running `command` agent.
