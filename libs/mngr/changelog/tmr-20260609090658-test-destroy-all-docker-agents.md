Fixed the e2e test fixture (`e2e/conftest.py`) so that the `settings.local.toml` it writes no
longer contains a duplicate `type = "claude"` key under `[commands.create]`. The duplicate key
caused tomlkit to fail config parsing ("Cannot overwrite a value"), which made every e2e tutorial
test (including the Docker tutorial tests) fail before running any `mngr` command.
