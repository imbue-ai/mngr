Fixed the `test_exec_git_log` tutorial e2e test: removed the superfluous
`@pytest.mark.modal` marker (the test exercises a local command agent and
never invokes Modal, which the resource guard flags), and strengthened its
assertions to verify the agent's `git log --oneline` output shows real commit
history rather than only checking the exit code.
