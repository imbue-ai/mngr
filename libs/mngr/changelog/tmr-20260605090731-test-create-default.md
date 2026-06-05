Removed the superfluous `@pytest.mark.modal` from the `test_create_default` e2e
tutorial test (it creates a local-provider agent and never invokes Modal, so the
resource guard flagged the mark as never-invoked). Strengthened the test to
verify the agent is actually running inside its worktree by execing `pwd` in the
agent and comparing it against the reported `work_dir`.
