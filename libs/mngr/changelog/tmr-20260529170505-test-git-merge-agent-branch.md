Removed the superfluous `@pytest.mark.modal` mark from the `test_git_merge_agent_branch`
e2e tutorial test. The test creates a local command agent and merges its branch, which
exercises rsync and tmux but never invokes Modal, so the resource guard failed the test.
Also strengthened the test to verify that an agent commit actually merges into the current
branch, rather than only running the merge command and ignoring its result.
