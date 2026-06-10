Fixed the e2e test fixture's `settings.local.toml`, which contained a duplicate
`type = "claude"` key under `[commands.create]` and was therefore invalid TOML.
Any e2e command that performed a full merged-config load (e.g. `mngr config
unset`) failed with a "Failed to parse config file" error instead of running.
Also tightened `test_config_unset_missing_key` to assert the error names the
specific missing key.
