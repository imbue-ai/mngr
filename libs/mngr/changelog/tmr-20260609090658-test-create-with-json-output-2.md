Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) which wrote a
duplicate `type = "claude"` key into the generated `settings.local.toml`, making the
file unparseable and causing every e2e `mngr` command to fail with "Cannot overwrite
a value". Removed the duplicate key.

Strengthened `test_create_with_json_output` to verify that the `agent_id`/`host_id`
returned by `mngr create --format json` actually match the agent reported by
`mngr list --format json`, and that the agent is in a running state -- confirming the
machine-readable identifiers are real and usable for scripting, not just well-formed.
