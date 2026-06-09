Fixed the e2e tutorial test fixture and the command-agent dev-server test.

- Removed a duplicate `type = "claude"` key in the `[commands.create]` section of the
  `settings.local.toml` written by the shared `e2e` fixture (`e2e/conftest.py`). The duplicate
  key was a merge artifact that made the TOML unparseable, so every e2e tutorial command that
  loaded the config failed with "Cannot overwrite a value".
- `test_command_agent_dev_server_extra_windows` is a purely local command-agent test, so it no
  longer carries a spurious `@pytest.mark.modal` (which the resource guard rejected as "marked
  but never invoked"). Its verification listing is now scoped to `--provider local`, mirroring
  the sibling `test_command_agent_python_http`; the Modal command-agent path remains covered by
  `test_command_agent_batch_job_modal`.
