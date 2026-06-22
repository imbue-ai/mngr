Fix the `test_event_follow` tutorial e2e test (and the shared `_create_my_task` helper comment) to stop claiming the local command agent uses rsync.

The test created a local command agent inside a git project, where mngr's default transfer is a git worktree, not rsync (rsync is only used for non-git projects). No `mngr` command in the test ever invoked rsync, so the resource guard correctly failed the test for carrying a superfluous `@pytest.mark.rsync` mark. Removed the mark and corrected the misleading comment that claimed local agents always rsync their work dir into place.
