Fixed the e2e tutorial test fixture so the `command`-type agent tutorial tests run again.

- The shared `e2e` fixture (`e2e/conftest.py`) was writing a `settings.local.toml` with a
  duplicate `type = "claude"` key under `[commands.create]`, which is invalid TOML and made
  every `mngr` invocation in the affected e2e tests fail with "Cannot overwrite a value". Removed
  the duplicate key.
- `test_create_command_python_http` now carries `@pytest.mark.timeout(120)` (matching its
  sibling `test_create_command_custom_script`), since creating a command agent plus the
  follow-up `mngr list`/`mngr exec` provider discovery can exceed the default 10s per-test
  timeout when a remote provider is unreachable.
