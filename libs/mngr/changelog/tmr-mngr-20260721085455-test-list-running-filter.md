Fixed the `test_list_running_filter` e2e tutorial test: added an explicit
`@pytest.mark.timeout(180)` so its five sequential `mngr` invocations no longer
trip the default 10s per-test timeout when run locally, and removed the
incorrect `@pytest.mark.rsync` mark (the local command-agent creates use a git
worktree and never invoke rsync).
