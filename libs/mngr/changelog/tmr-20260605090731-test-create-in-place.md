Fixed the `test_create_in_place` release e2e test: added an explicit
`@pytest.mark.timeout(120)` so the test's multiple serial `mngr` subprocess
invocations are not killed by the default 10s per-test timeout, and removed the
superfluous `@pytest.mark.modal` mark (the in-place `--transfer=none` flow runs
entirely on the local provider and never invokes Modal). Also strengthened the
test to confirm at runtime, via `mngr exec my-task pwd`, that the agent process
actually runs in the source directory rather than relying on `mngr list`
metadata alone.
