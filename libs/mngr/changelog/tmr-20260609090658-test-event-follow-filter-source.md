Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that wrote a
duplicate `type = "claude"` key into the generated `settings.local.toml`,
producing invalid TOML that made every tutorial e2e agent-creation command fail
with "Cannot overwrite a value". The fixture now writes the default agent type
once. This unblocks the `mngr event` tutorial e2e tests (including
`test_event_follow_filter_source`).
