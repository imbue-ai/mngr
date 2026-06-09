Fixed the e2e test fixture that wrote a duplicate `type = "claude"` key into the
generated `settings.local.toml`, which produced an invalid-TOML parse error
("Cannot overwrite a value") and broke every e2e command (e.g. `mngr create`).

Strengthened `test_rename_dry_run_does_not_rename` to also verify (via `mngr
exec`) that the agent remains reachable and running its command under its
original name after a dry-run, not just that the name is unchanged in
`mngr list`.
