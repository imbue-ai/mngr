Fixed the e2e tutorial test for `mngr config unset`. The shared e2e fixture
wrote a `settings.local.toml` with a duplicate `type = "claude"` key under
`[commands.create]`, which made `tomllib` reject the file and broke config
loading for every e2e mngr command. Removed the duplicate. Also reworked
`test_config_unset` to set `commands.create.provider` before unsetting it (a
key that is never set cannot be unset) and to verify the value is actually
removed from the project settings file.
