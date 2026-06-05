Fixed the `test_exec_git_status_short` e2e tutorial test: removed the superfluous
`@pytest.mark.modal` marker (the test only creates a local command-type agent and
never invokes Modal, so the resource guard failed it), and strengthened it to
deterministically create an uncommitted file in the agent's workspace and assert
that `git status --short` reports it.
