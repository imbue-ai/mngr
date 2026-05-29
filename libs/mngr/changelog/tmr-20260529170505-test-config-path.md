Strengthened the `mngr config path` tutorial e2e test to assert on the actual
command output (all three scopes reported, each pointing at a TOML settings
file with an existence status) instead of only checking the exit code, and
added an unhappy-path test verifying that an invalid `--scope` value is
rejected.
