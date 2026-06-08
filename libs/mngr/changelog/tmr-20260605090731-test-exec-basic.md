Removed the superfluous `@pytest.mark.modal` mark from the `test_exec_basic`
e2e tutorial test. The test creates a single local command agent and runs
`mngr exec` against it by name, a path that never invokes Modal, so the
resource guard failed the test for declaring a Modal dependency it did not
exercise. The `rsync` and `tmux` marks remain because local `mngr create`
genuinely invokes both.

Also strengthened the assertion to verify that `mngr exec` forwards the
command's stdout back from the agent's host (checking for the leading
"total" line of `ls -la`), rather than only checking the exit code.
