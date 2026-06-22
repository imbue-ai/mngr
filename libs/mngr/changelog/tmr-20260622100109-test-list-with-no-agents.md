Hardened the `mngr list` tutorial e2e tests so they pass deterministically regardless of which provider plugins are installed in the test environment:

- The e2e fixture now scopes provider discovery to the backends the environment can actually reach (`local`, `modal`, and `docker` only when a daemon is reachable). The dev workspace installs every provider plugin, so an unconfigured cloud backend (e.g. AWS) would otherwise raise `ProviderUnavailableError` and make a bare `mngr list` exit non-zero.

- `test_list_with_no_agents` now sets an explicit per-test timeout (matching `test_list_local_filter`), since full provider discovery routinely exceeds the default 10s per-test timeout.
