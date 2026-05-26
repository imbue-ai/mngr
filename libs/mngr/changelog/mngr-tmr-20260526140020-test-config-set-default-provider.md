## mngr

- Tightened `test_config_set_default_provider` e2e test: the `mngr config set` output assertion now uses a structured regex (`Set <key> = <value>`) and verifies the scope is reported, and the test now also reads `.<root>/settings.toml` directly to confirm the value is actually persisted to disk as TOML.
