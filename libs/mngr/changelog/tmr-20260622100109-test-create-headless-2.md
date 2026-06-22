Test-only: fixed the `test_create_headless` BASIC-CREATION e2e tutorial test so it passes in environments where a cloud provider is installed-but-unconfigured.

The verification `mngr list` is now scoped to `--provider local` (matching the sibling agent-type tests), so it no longer full-scans every registered backend; an enabled-but-unconfigured cloud provider (e.g. AWS with no credentials) raises `ProviderUnavailableError`, which made a bare `mngr list` exit non-zero even though the local agent was created fine.

Removed the test's stale `@pytest.mark.rsync` mark: the test creates a local agent with the default git-worktree transfer (built with `git worktree add`, not rsync) and local deploy-file upload is a plain copy, so rsync is never invoked and the resource guard's NEVER_INVOKED check failed once the test got far enough to pass.
