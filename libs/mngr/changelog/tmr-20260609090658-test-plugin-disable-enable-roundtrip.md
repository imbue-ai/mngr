Fixed the e2e test fixture and the plugin e2e tests so the plugin disable/enable roundtrip
release test runs correctly:

- Removed a duplicate `type = "claude"` key from the `[commands.create]` table that the e2e
  fixture writes into `settings.local.toml`. The duplicate produced invalid TOML, so every
  command in an affected e2e test failed up front with "Cannot overwrite a value".
- Added `@pytest.mark.timeout(300)` to the two plugin e2e tests, matching the convention used
  by other multi-command e2e release tests. Their several real CLI subprocess invocations
  exceed the global 10s `func_only` timeout.
