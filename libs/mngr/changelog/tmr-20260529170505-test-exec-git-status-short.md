Fixed the `test_exec_git_status_short` e2e tutorial test: removed the superfluous
`@pytest.mark.modal` mark. The test creates a local command agent (default
provider), which never invokes the Modal CLI, so the mark tripped the resource
guard's "marked but never invoked" check. The test now also verifies the actual
behavior of `mngr exec my-task "git status --short"` by creating a known
uncommitted change in the agent's checkout and asserting that the porcelain
output reports it.
