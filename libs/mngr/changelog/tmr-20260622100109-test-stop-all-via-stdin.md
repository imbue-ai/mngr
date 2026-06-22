Fixed the `test_stop_all_via_stdin` tutorial e2e test (covering `mngr list --ids | mngr stop -`):

- Added a 120s timeout so the stdin-piped two-process command does not hit the default 10s pytest timeout.

- Scoped the post-stop verification `mngr list` queries to `--provider local`, so an unconfigured remote provider (e.g. AWS) in the test environment no longer makes the verification exit non-zero.

- Removed the superfluous `@pytest.mark.rsync` mark: the test only creates a local git-repo agent (populated via git-worktree, not rsync) and stops it, so it never exercises rsync.
