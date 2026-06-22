Fixed the `test_exec_git_status_short` e2e tutorial test (covering `mngr exec my-task "git status --short"`).

The test was missing a `@pytest.mark.timeout` override, so it inherited the 10s default and timed out inside `mngr create` (which takes ~30s+); it now uses `timeout(120)` like its agent-creating siblings.

It also carried a stale `@pytest.mark.rsync` mark. These tutorial tests run against a git repo, so the agent is provisioned via a git worktree (`GIT_WORKTREE`), never rsync (which is only used for non-git projects). The resource guard correctly flagged the mark as never invoked; it has been removed.

Tightened the assertion to check for the `?? uncommitted_change.txt` porcelain prefix rather than just the filename, confirming `git status --short` actually emits short/porcelain output.
