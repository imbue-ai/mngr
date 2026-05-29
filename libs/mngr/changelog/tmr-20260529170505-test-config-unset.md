Fixed the `test_config_unset` tutorial e2e test, which previously failed because
`mngr config unset` defaults to the project scope and the key did not exist there
in the isolated test environment. The test now sets the key as a precondition,
verifies it is present, runs the tutorial `mngr config unset` command, and
verifies the key is actually removed. Also added a `test_config_unset_missing_key`
test covering the unhappy path where the key is absent.
