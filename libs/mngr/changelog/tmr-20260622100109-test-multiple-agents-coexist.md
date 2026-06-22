Test-only: fixed the e2e release test `test_multiple_agents_coexist`.

Scoped its `mngr list` call to `--provider local` so that an enabled-but-unconfigured cloud provider (e.g. AWS without credentials, which raises `ProviderUnavailableError`) no longer makes the command exit non-zero. The test only creates local agents, so local-scoped discovery is both sufficient and faithful, matching the sibling e2e tests.

Removed the now-stale `@pytest.mark.rsync` mark: the test runs against a clean git repo, so `mngr create` takes the git-worktree path with no uncommitted/untracked files to transfer, and rsync is never invoked. The resource guard flagged the superfluous mark once the test body began passing.
