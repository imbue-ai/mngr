Fixed the e2e tutorial test suite so unfiltered `mngr list` tests pass in environments without cloud credentials or Docker.

The e2e fixture now restricts discovery to the backends each test can actually reach: `local` is always enabled, while `modal` and `docker` are enabled only when the test carries the matching `@pytest.mark.modal` / `@pytest.mark.docker` mark. Previously the fixture left `enabled_backends` empty, which enabled every registered backend (aws, azure, gcp, ...); an unfiltered `mngr list` would then try to reach those uncredentialed providers and exit with `EXIT_CODE_PROVIDER_INACCESSIBLE` or hang on their discovery timeouts.

Also dropped the spurious `@pytest.mark.rsync` mark from `test_advanced_watch_list_live_dashboard`, which creates a local command agent and never invokes rsync.
