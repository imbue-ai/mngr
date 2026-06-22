Fixed the `test_exec_no_start` e2e tutorial test (covering `mngr exec --no-start`):

- Added a `@pytest.mark.timeout(180)` mark so agent creation is no longer killed by the global 10s per-test timeout default.

- Removed the incorrect `@pytest.mark.rsync` mark: a local `--type command` agent uses a git-worktree work_dir and never invokes rsync, so the resource guard correctly flagged the unused mark.
