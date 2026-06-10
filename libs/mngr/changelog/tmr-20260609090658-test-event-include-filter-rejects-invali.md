Fixed the e2e tutorial test fixture, which wrote an invalid `settings.local.toml` containing a
duplicate `type = "claude"` key under `[commands.create]`. This caused every tutorial e2e test
that creates an agent to fail with a TOML parse error ("Cannot overwrite a value"). The duplicate
line was removed so the generated config is valid.

Also strengthened `test_event_include_filter_rejects_invalid_cel` to assert that a rejected
`--include` CEL filter produces no event output on stdout, verifying the command fails loudly
rather than silently emitting events.
