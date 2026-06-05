Fixed the `test_destroy_multiple_at_once` e2e release test, which destroys
multiple agents in a single `mngr destroy agent-1 agent-2 agent-3 --force`
command. The test was hitting the global 10s pytest timeout (it creates three
agents and destroys them), so it now carries an explicit `@pytest.mark.timeout(120)`.
The misapplied `@pytest.mark.modal` mark was removed because the test exercises
only local command agents and never invokes Modal. Also strengthened the test
to verify all three agents exist before the destroy, that each is reported as
destroyed, and that none remain afterward. No mngr behavior change.
