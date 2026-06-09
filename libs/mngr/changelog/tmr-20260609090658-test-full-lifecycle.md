Fixed the e2e `test_full_lifecycle` release test (and the shared e2e fixture it depends on):

- Removed a duplicate `type = "claude"` key under `[commands.create]` in the e2e conftest's
  generated `settings.local.toml`. The duplicate made tomlkit refuse to parse the file
  ("Cannot overwrite a value"), causing every `mngr` command in e2e tests to fail at config
  load.
- Added `@pytest.mark.timeout(300)` to `test_full_lifecycle`, matching the convention used by
  the other multi-command e2e tests. Without it the test inherited the global 10s `func_only`
  timeout and was killed partway through its sequence of `mngr` commands.
