Improved the test suite for the top-level `imbue/minds` modules (bootstrap, CLI
entrypoint, claude-version alignment):

- `test_cli_shows_help` now asserts the top-level CLI lists its real subcommands
  (`run`, `pool`, `env`) and the group identity line, instead of checking for an
  incidental word ("forward") that only appeared in one subcommand's help text.
- `test_legacy_devminds_value_falls_back_with_warning` now captures loguru output
  and asserts the operator-facing warning actually fires (naming the bad value),
  matching what the test's name and docstring promised.
- Added tests for the previously-unexercised branches of `_ensure_mngr_settings`:
  removal of the legacy `[providers.ssh]` block plus deletion of the stale
  `ssh/dynamic_hosts.toml` / `ssh/keys/leased_host/` artifacts, and the no-op
  short-circuit when settings are already in the desired shape.
- Replaced a dead `if settings_path.exists(): unlink()` guard with a positive
  `assert not settings_path.exists()` precondition in the "creates settings file
  when missing" test.
- Documented why the `minds_data_dir_for` / `mngr_host_dir_for` / `mngr_prefix_for`
  derivation tests pin the on-disk layout contract (so they are not mistaken for
  redundant constructor-echo tests).
- Marked the network-dependent release test
  `test_claude_code_version_matches_forever_claude_template_pin` as
  `@pytest.mark.flaky` so transient GitHub fetch failures are retried.
