Fix the `mngr list --limit` e2e release test (`test_list_limit`) so it passes in an environment where cloud backends (e.g. AWS) are installed but not configured.

The two verification listings were scoped to `--provider local` (the provider the test agents actually run on), matching the pattern already used by the other local-agent e2e tests. The unscoped listing previously aborted with a non-zero exit when default discovery reached the unconfigured `aws` backend, which raises `ProviderUnavailableError` under the default `--on-error abort`.

Also removed the superfluous `@pytest.mark.rsync` mark: the test creates local agents (git-worktree based), which never invoke rsync, so the resource guard rejected the mark once the test body began passing.
