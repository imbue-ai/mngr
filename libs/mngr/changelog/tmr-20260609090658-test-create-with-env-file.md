Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) so it no
longer writes a duplicate `type = "claude"` key into the per-test
`settings.local.toml`. The duplicate made tomlkit reject the config file
("Cannot overwrite a value"), causing every e2e tutorial test that creates an
agent to fail. This affected `test_create_with_env_file` and its siblings in
`test_env_vars.py`.

Also added `test_create_with_missing_env_file_is_rejected`, an unhappy-path e2e
test covering the same `--env-file` tutorial block: it verifies that pointing
`--env-file` at a nonexistent file is rejected with a clear error and creates no
agent.
