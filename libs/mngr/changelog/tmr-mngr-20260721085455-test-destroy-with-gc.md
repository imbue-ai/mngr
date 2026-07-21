Removed a superfluous `@pytest.mark.rsync` mark from the `test_destroy_with_gc`
e2e tutorial test. The test creates a local `--type command` agent with
`--no-connect` and never invokes rsync, so the mark tripped the rsync resource
guard's "marked but never invoked" check and failed the test.
