Fixed the `test_message_multiple_agents_by_name` e2e tutorial test: removed the
superfluous `@pytest.mark.modal` mark (the test only creates local `command`
agents and messages them by name, so Modal is never invoked and the resource
guard rejected the mark). Also strengthened the test to assert that all three
named agents are reported as having received the message.
