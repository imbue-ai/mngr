Fixed the e2e test fixture, which wrote a `settings.local.toml` containing a
duplicate `type = "claude"` key under `[commands.create]`. TOML rejects a
repeated key, so every `mngr` command run through the e2e fixture failed with a
config parse error. Removing the duplicate restores the e2e suite.

Strengthened `test_create_help_succeeds` to assert that `mngr create --help`
emits the command's own NAME summary, SYNOPSIS, and EXAMPLES sections (not just
two flag strings), confirming the help genuinely belongs to the `create` command.
