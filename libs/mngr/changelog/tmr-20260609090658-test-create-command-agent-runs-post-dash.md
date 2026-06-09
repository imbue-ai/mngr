Fixed the e2e tutorial test fixture and the `--type command -- <cmd>` create test.

- Removed a duplicate `type = "claude"` key under `[commands.create]` in the e2e
  `settings.local.toml` that the fixture writes (`e2e/conftest.py`). The duplicate key made
  the file invalid TOML, so every `mngr` invocation in an e2e test aborted with
  "Cannot overwrite a value".
- Added `@pytest.mark.timeout(120)` to
  `test_create_command_agent_runs_post_dash_command_in_agent`, matching its sibling
  real-create tests. A real create (tmux session + asciinema connect, plus a one-time ttyd
  install) followed by `mngr exec` and `mngr list` routinely exceeds the default 10s
  function timeout.
