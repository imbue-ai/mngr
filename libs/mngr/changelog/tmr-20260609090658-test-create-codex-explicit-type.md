Fixed the e2e test fixture that seeded `settings.local.toml` with a duplicate
`type = "claude"` key under `[commands.create]`. The duplicate parsed on initial
load but caused `mngr config set --scope local` to fail with "Cannot overwrite a
value" when it re-saved the file, breaking `test_create_codex_explicit_type`.
