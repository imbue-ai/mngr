Fixed the `test_create_agent_args_require_dash_separator` e2e release test (no user-visible change).

It was missing the `@pytest.mark.timeout(120)` mark its sibling tests carry, so it tripped the default 10s pytest-timeout while `mngr` performed provider discovery during startup. Its verification listing also now uses `mngr list --provider local`, since the test only ever creates a local (`--type command`) agent; this keeps the parse-error test independent of whichever remote providers (a running Docker daemon, configured cloud backends) happen to be reachable in the environment.
