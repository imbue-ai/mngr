Fixed a duplicate `type = "claude"` key in the e2e test fixture's
`settings.local.toml` that made the file invalid TOML, causing
`test_create_with_label` (and any e2e test loading local config) to fail with
"Cannot overwrite a value".

Extended `test_create_with_label` to verify labels and host labels actually
drive `mngr list` filtering (matching values include the agent, non-matching
values exclude it).
