Fixed the `test_create_command_python_http` e2e tutorial test: it was marked
`@pytest.mark.modal` but only creates a local `command`-type agent, so it never
invoked Modal and failed the resource guard. Removed the spurious mark and added
verification that the agent is actually created with the expected command and is
running inside the agent.
