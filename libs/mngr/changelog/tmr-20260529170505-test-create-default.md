- Fixed the `test_create_default` tutorial e2e test: removed the spurious `@pytest.mark.modal`
  mark. The test only creates a local agent and never invokes the `modal` CLI, so the resource
  guard failed the test with "marked with @pytest.mark.modal but never invoked modal". The
  `rsync` mark is retained because the default worktree create performs a same-host rsync of the
  working tree.
- Strengthened `test_create_default` to also assert the agent's host uses the local provider (the
  documented default) and that the agent actually runs inside its worktree (via
  `mngr exec my-task pwd`), not merely that it is listed with that work directory.
