Fixed the `test_destroy_multiple_at_once` e2e tutorial test: added a 120s
`@pytest.mark.timeout` override (the 10s default was too short for creating and
destroying three agents) and removed the incorrect `@pytest.mark.modal` mark
(the test exercises only the local provider and never invokes Modal). Also
strengthened the test to assert that each named agent is reported as destroyed
and that the command reports destroying all three agents.
