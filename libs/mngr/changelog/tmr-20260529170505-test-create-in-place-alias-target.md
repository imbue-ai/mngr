Fixed the `test_create_in_place_alias_target` e2e release test: removed the
incorrect `@pytest.mark.modal` mark (the test creates a purely local in-place
agent and never invokes Modal) and added `@pytest.mark.timeout(120)` so the
test body has enough time to create the agent, list it, and verify it runs
in-place (the default 10s timeout was too short).
