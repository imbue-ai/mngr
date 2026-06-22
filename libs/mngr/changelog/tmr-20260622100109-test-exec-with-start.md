Removed the spurious `@pytest.mark.rsync` from the `test_exec_with_start` e2e
tutorial test. The test creates a local command agent (git-worktree transfer)
and runs `mngr exec --start` on the local host, neither of which invokes rsync,
so the resource guard failed the otherwise-passing test with "marked rsync but
never invoked rsync".
