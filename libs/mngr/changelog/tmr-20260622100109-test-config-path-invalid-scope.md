Fixed the `test_config_path_invalid_scope` e2e tutorial test, which intermittently failed because a single `mngr config path` subprocess cold start could exceed the default 10s per-test timeout; it now carries the same `@pytest.mark.timeout(60)` budget as the other multi-second `mngr` config tests.

Strengthened the test's assertions to verify that an invalid `--scope` value is echoed back and that all supported scopes (`user`, `project`, `local`) are listed in the error, matching the documented behavior.
