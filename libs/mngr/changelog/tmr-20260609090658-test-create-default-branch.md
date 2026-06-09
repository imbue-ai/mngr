Fixed the e2e tutorial test fixture (`e2e/conftest.py`) that wrote an invalid
`settings.local.toml` with a duplicate `type = "claude"` key under
`[commands.create]`, which caused `mngr create` to fail with a TOML parse error
("Cannot overwrite a value") in every e2e tutorial test. Also added a
`@pytest.mark.timeout(120)` mark to `test_create_default_branch`, which was
exceeding the default 10s per-test timeout because it runs `mngr create` plus
several `mngr exec` commands.
