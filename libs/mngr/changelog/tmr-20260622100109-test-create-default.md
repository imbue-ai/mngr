Fixed the `test_create_default` tutorial e2e test (BASIC CREATION):

- The e2e fixture now pins `enabled_backends` to only the providers each test exercises (`local` always; `modal`/`docker` only when the test carries the matching mark). The monorepo installs every provider plugin, so an enumerate-all `mngr list` would otherwise abort when an unconfigured/unavailable cloud backend (e.g. AWS with no credentials) raised `ProviderUnavailableError`.

- `test_create_default` now seeds an uncommitted file in the source repo before `mngr create` and asserts it is rsynced into the new worktree, exercising the uncommitted-work transfer that the `@pytest.mark.rsync` mark declares.
