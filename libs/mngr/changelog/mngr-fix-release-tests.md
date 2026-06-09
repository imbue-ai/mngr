Fixed a regression in the e2e test fixture that wrote a malformed `settings.local.toml`
(a duplicated `type = "claude"` key under `[commands.create]`). The duplicate key made
every `mngr` invocation in the e2e/tutorial release suite fail to parse its config and
exit non-zero, cascading into a large block of release-test failures.

Also updated two e2e tutorial tests (`test_config_set_unknown_key_fails`,
`test_config_set_rejects_unknown_key`) that assumed the project `settings.toml` does not
exist until a command writes it. The e2e fixture now intentionally pre-seeds that file
with the pytest opt-in key, so these tests now verify that a rejected `config set` leaves
the file unchanged (and never writes the bad key) rather than asserting the file is absent.

All test-only changes; they do not change `mngr`'s runtime behavior.
