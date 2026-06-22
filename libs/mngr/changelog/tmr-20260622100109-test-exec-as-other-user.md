Fix the release e2e tutorial test `test_exec_as_other_user` (EXEC section).

The test was marked `@pytest.mark.rsync`, but `mngr exec` never invokes rsync and the agent it creates uses the git-worktree transfer mode (the e2e working dir is a git repo), so create does not invoke rsync either. The resource guard therefore failed the otherwise-passing test with "marked with @pytest.mark.rsync but never invoked rsync". Removed the superfluous mark. The `@pytest.mark.tmux` mark is retained because `mngr create --type command` starts the agent inside a tmux session. No production behavior change.
