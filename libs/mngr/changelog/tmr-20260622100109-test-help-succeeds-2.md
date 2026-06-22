Fixed the tutorial help e2e tests (`test_help.py`) timing out under the default 10s per-test limit: the first `mngr` invocation pays the CLI's ~10s import/startup cost, so each test now carries a `@pytest.mark.timeout(120)` override matching the other tutorial tests.

Also tightened `test_help_succeeds` to assert that `mngr --help` emits nothing on stderr, catching warning/deprecation regressions on the main entrypoint (consistent with `test_create_help_succeeds`).
