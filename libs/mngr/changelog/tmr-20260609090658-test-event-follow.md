Fixed the e2e tutorial test fixture so agent creation works again. The shared
`settings.local.toml` written by the e2e session fixture contained a duplicate
`type = "claude"` key under `[commands.create]`, which made the config file
invalid TOML and caused every `mngr create` in the tutorial e2e tests to fail
with "Cannot overwrite a value". Removed the duplicate key.

Also removed a redundant second definition of the `_parse_jsonl_events` helper
in `test_event.py` that shadowed the stronger original (the original asserts
each parsed line is a JSON object).
