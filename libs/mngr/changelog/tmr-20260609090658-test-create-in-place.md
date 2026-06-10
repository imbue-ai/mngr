Fixed the e2e test fixture that seeded a malformed `settings.local.toml` with a
duplicate `type = "claude"` key under `[commands.create]`, which made every
`mngr create` invoked from an e2e tutorial test fail with a TOML "Cannot
overwrite a value" parse error. Removed the duplicate key.
