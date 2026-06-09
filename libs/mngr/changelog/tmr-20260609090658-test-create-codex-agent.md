Fixed the `test_create_codex_agent` e2e tutorial test. The e2e fixture was
writing a duplicate `type = "claude"` key into `[commands.create]` in the
generated `settings.local.toml`, which produced invalid TOML and broke any
command (such as `mngr config set --scope local`) that loads and re-saves that
file via tomlkit. Removed the duplicate. Also added a `@pytest.mark.timeout(120)`
to the test (it runs three sequential mngr operations that each perform full
provider discovery, exceeding the default 10s timeout) and removed the spurious
`@pytest.mark.modal` (the test only creates a local agent and never invokes the
modal CLI binary the resource guard tracks).
