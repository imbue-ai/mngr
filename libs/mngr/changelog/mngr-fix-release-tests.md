Fixed a regression in the e2e test fixture that wrote a malformed `settings.local.toml`
(a duplicated `type = "claude"` key under `[commands.create]`). The duplicate key made
every `mngr` invocation in the e2e/tutorial release suite fail to parse its config and
exit non-zero, cascading into a large block of release-test failures. This is a
test-only fix; it does not change `mngr`'s runtime behavior.
