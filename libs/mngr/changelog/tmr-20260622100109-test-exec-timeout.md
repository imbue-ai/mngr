Remove the spurious `@pytest.mark.rsync` mark from the `test_exec_timeout` e2e tutorial test.

The test only creates a local `command`-type agent and runs `mngr exec my-task --timeout 30 "echo done"`, which never invokes rsync. The resource guard's unused-mark check (which fires only on otherwise-passing tests) therefore failed the test with "marked with @pytest.mark.rsync but never invoked rsync". Dropping the unused mark lets the test pass while keeping the marks accurate.
