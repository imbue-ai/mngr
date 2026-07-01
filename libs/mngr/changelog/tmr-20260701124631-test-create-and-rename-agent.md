Fixed the `mngr rename` end-to-end release tests (`test_create_and_rename_agent`, `test_rename_dry_run_does_not_rename`) so they run correctly outside the Docker-enabled release sandbox.

The shared e2e fixture now disables the credential-requiring cloud providers the suite never exercises (aws, azure, gcp, ovh, vultr, imbue_cloud). Their backends ship in this monorepo, so `mngr list` and other discovery paths would otherwise load a default instance for each and abort with `ProviderUnavailableError` when no credentials are present. The docker provider is likewise disabled when no daemon socket is reachable (e.g. local runs outside the release sandbox), and left enabled where a daemon is available so tests still exercise the full discovery path.

Also removed a spurious `@pytest.mark.rsync` from both rename tests: they create a local command agent (which uses a git worktree, not rsync), so the mark tripped the resource guard's mark-without-invocation check.
