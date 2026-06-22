Fix the `mngr rename` e2e release tests (`test_rename.py`) so they pass in an environment where cloud backends (e.g. AWS) are installed but not configured.

The verification step scoped `mngr list --format json` to `--provider local` (the provider the test agent actually runs on), matching the pattern already used by the other local-agent e2e tests. The unscoped listing previously aborted with a non-zero exit when the default discovery reached the unconfigured `aws` backend, which raises `ProviderUnavailableError`.

Also removed the superfluous `@pytest.mark.rsync` mark from both rename tests: they create local agents (git-worktree based), which never invoke rsync, so the resource guard rejected the mark once the test body began passing.
