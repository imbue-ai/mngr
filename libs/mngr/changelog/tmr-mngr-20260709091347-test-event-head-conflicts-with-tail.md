Dropped the over-declared `@pytest.mark.rsync` from the `mngr event --head --tail`
conflict e2e test. That test's command fails during argument validation before any
events are read, and creating the local command agent against a git repo uses a git
worktree rather than rsync, so rsync is never invoked and the resource guard was
failing the test. Flagged the same over-declaration on the sibling event tests for a
follow-up suite-wide cleanup.
