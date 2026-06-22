Test-only: fixed the `mngr list` tutorial e2e tests (`libs/mngr/imbue/mngr/e2e/tutorial/test_list.py`) so a bare `mngr list` succeeds in the test environment.

The e2e fixture now pins `enabled_backends` to the providers the tutorials actually exercise (local, ssh, modal, and docker when a daemon is reachable). The monorepo's `uv sync --all-packages` installs cloud-provider plugins (aws, azure, gcp, ...) that are not dependencies of the published `mngr` wheel; left enabled, an unconfigured cloud provider raised `ProviderUnavailableError`, which landed in the listing's `errors` and made every bare `mngr list` exit non-zero.

Added `@pytest.mark.timeout(60)` to `test_list_project_filter` (the full discovery path routinely takes ~10s, past the default 10s per-test timeout), and added `test_list_project_filter_matches`, a happy-path test that verifies `--project` includes an agent tagged with that project label and excludes agents tagged with a different one.
