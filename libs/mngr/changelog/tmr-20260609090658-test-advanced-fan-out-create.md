Fixed the e2e test fixture's generated `settings.local.toml`, which defined the
`type` key twice under `[commands.create]`. The duplicate key produced invalid
TOML ("Cannot overwrite a value"), causing every `mngr` invocation in e2e
tutorial tests to fail while parsing the config file.
