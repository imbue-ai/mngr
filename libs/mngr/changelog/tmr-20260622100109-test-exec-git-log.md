Removed an incorrect `@pytest.mark.rsync` from the `test_exec_git_log` e2e tutorial test.

The test creates a local git agent (which uses a git worktree, never rsync) and runs `mngr exec my-task "git log --oneline -5"`. Since rsync is never invoked, the resource guard correctly failed the test for carrying a `rsync` mark it never exercised. Removing the mark fixes the failure; the test's behavior and assertions are unchanged.
