Fixed the tutorial e2e test suite so it no longer fails on provider backends that cannot be reached in the test environment.

The e2e fixture now restricts `enabled_backends` to the backends the tests actually exercise: `local` and `modal` always, plus `docker` only when a Docker daemon is reachable. Previously the dev monorepo installed every provider plugin, so `mngr list` (and every other list-based command) also tried the credential-requiring cloud backends (aws, azure, gcp, vultr, imbue_cloud). Those surface a `ProviderUnavailableError` when uncredentialed, which made `mngr list` exit non-zero and failed every list-based e2e test.

Also removed a superfluous `@pytest.mark.rsync` from `test_create_and_destroy_agent`: it creates a local agent against a clean repo, which never invokes rsync.
