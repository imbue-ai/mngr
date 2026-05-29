Fixed the `test_exec_force_commit` e2e tutorial test (WORKING WITH GIT section).
It was hitting the global 10s pytest timeout during local agent creation
because it lacked a `@pytest.mark.timeout` override, and it carried a
superfluous `@pytest.mark.modal` mark that the resource guard rejected since
the test only creates a local command agent. Added a 180s timeout marker,
removed the modal mark, and strengthened the assertions to verify the forced
commit actually lands on the agent's branch.
