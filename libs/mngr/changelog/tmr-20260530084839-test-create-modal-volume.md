Fixed the `test_create_modal_volume` release e2e test, which was failing because
the isolated test environment has no configured default agent type (after the
implicit "claude" fallback was removed). The test now passes `--type claude`
explicitly, and additionally verifies that the persistent Modal volume is
actually mounted at the requested target path on the remote host.
