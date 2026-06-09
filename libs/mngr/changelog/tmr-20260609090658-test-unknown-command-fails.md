Fixed the e2e tutorial test fixture and strengthened the unknown-command test.

- The e2e `settings.local.toml` written by the tutorial-test fixture contained a duplicate
  `type = "claude"` key under `[commands.create]` (a merge artifact). This is invalid TOML and
  made every e2e tutorial command abort with a config parse error ("Cannot overwrite a value")
  instead of running. Removed the duplicate so the fixture config parses again.
- `test_unknown_command_fails` now asserts the precise Click usage exit code (2), that stdout is
  empty (clean for scripting), and that the error names the offending command, in addition to
  pointing the user back at `mngr --help`.
