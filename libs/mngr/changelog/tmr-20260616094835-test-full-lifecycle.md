Fixed the `test_full_lifecycle` e2e release test so it no longer depends on cloud credentials or a running Docker daemon being present in the test environment.

The e2e fixture now pins `enabled_backends` to the backends the suite actually exercises (local and modal, plus docker only when a docker daemon is reachable). Previously, because the test repo is built with `uv sync --all-packages`, optional provider plugins (e.g. imbue-mngr-aws) registered default backend instances that raise `ProviderUnavailableError` when their credentials are missing, which made every `mngr list` exit non-zero even though the listing itself was correct.

Also removed a spurious `@pytest.mark.rsync` marker from `test_full_lifecycle`: it creates a local command agent that transfers via git-worktree and never invokes rsync, so the resource guard flagged the unused marker.
