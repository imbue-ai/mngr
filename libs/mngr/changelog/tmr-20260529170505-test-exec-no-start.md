Removed the superfluous `@pytest.mark.modal` from the `test_exec_no_start`
e2e tutorial test: it creates a local command agent and runs `mngr exec
--no-start` against it, so the Modal CLI is never invoked and the resource
guard rejected the mark. Also strengthened the assertion to verify the
executed command's output (`/etc/os-release` content) rather than only the
exit code.
