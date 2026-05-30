Fixed the `test_multiple_agents_coexist` e2e release test: added a 180s
`@pytest.mark.timeout` override (the global 10s default was too short for
creating three agents) and removed the superfluous `@pytest.mark.modal` mark
(the test only creates local `command` agents and never invokes Modal, which
tripped the resource-guard "marked but never invoked" check). Also strengthened
the test to assert each agent runs in its own distinct worktree.
