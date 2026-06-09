Fixed a duplicate `type = "claude"` key in the e2e test fixture's generated
`settings.local.toml` that produced invalid TOML ("Cannot overwrite a value"),
causing `mngr` config loading to fail in e2e tutorial tests. Also strengthened
the `test_control_mngr_via_env_rejects_invalid_value` release test to assert on
the specific "Unknown provider backend" rejection message.
