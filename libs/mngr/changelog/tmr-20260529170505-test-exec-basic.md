Fixed the `mngr exec` tutorial e2e test `test_exec_basic`: removed the incorrect
`@pytest.mark.modal` (the test exercises a local command agent and never touches
Modal), strengthened it to assert that the forwarded command's stdout is actually
returned, and added `test_exec_propagates_command_failure` covering the unhappy
path where a failing command yields a non-zero `mngr exec` exit code.
