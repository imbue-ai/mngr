Fixed the `test_create_with_message` e2e tutorial test: removed the spurious
`@pytest.mark.modal` mark. The test creates a local `command` agent and never
invokes the Modal CLI, so the resource guard failed it for being marked
`modal` without exercising Modal. Also strengthened the test to assert the
agent reaches a live state with the expected command, verifying the full
initial-message create path (readiness handshake then message send) rather
than just that the agent exists.
