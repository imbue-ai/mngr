Removed the superfluous `@pytest.mark.modal` from the `test_list_json_with_no_agents` e2e
release test. Listing agents in a fresh, empty environment never invokes Modal, so the
resource guard flagged the mark as never-invoked and failed the otherwise-passing test.
