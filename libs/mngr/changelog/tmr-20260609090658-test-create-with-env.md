Fixed the e2e tutorial test `test_create_with_env` and its shared fixture:

- Removed a duplicate `type = "claude"` key in the e2e `settings.local.toml`
  written by the `e2e` fixture, which made the file invalid TOML and caused
  every `mngr create` in the e2e tutorial tests to fail with a config parse
  error.
- Reworked the `--env` test to launch its agent body via `bash -c '...'` so the
  compound command and `$MNGR_TEST_VAR` expansion run inside the agent's shell,
  instead of being collapsed into a single (non-existent) command word by the
  command agent's per-argument shell quoting.
