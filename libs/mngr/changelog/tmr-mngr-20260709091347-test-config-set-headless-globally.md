Fixed the `test_config_set_headless_globally` e2e tutorial test to override the
10s global pytest timeout (which the `mngr` subprocess cold-start could exceed)
with `@pytest.mark.timeout(180)` and matching per-command subprocess timeouts,
consistent with the other `mngr`-invoking tests in the same module.
