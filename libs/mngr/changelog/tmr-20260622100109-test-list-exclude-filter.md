Test-only: fixed the `test_list_exclude_filter` e2e tutorial test and hardened the shared e2e fixture.

- Added the missing `@pytest.mark.timeout(180)` to `test_list_exclude_filter` (it was inheriting the 10s default and timing out during agent creation) and dropped its inaccurate `@pytest.mark.rsync` mark (the test uses local command agents that never invoke rsync).

- Pinned `enabled_backends` in the e2e fixture so the subprocess `mngr` only loads the backends the test environment actually supports (`local` and `modal` always; `docker` only for `@pytest.mark.docker` tests). Previously every registered backend was loaded, so unconfigured cloud backends (aws, azure, gcp, vultr, imbue_cloud) raised `ProviderUnavailableError` during discovery and made `mngr list` exit non-zero, failing any test that asserts the command succeeds.
