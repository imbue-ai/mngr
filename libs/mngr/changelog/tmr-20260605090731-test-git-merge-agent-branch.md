Fixed the `test_git_merge_agent_branch` e2e release test: removed the
superfluous `@pytest.mark.modal` marker (the test only creates a local command
agent and merges its branch, so it never invokes Modal and the resource guard
failed it). Also strengthened the test to merge real agent work and verify the
merged file appears in the caller's working tree.
