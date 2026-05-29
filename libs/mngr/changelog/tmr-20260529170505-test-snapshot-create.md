Fixed the `MANAGING SNAPSHOTS` e2e tutorial tests (`test_snapshot.py`): the
shared modal-agent setup helper now passes an explicit `--type command` (the
isolated test environment has no default agent type) and the tests that create
a modal agent are marked with `@pytest.mark.rsync` so the create-time file
transfer passes the test resource guard. Also strengthened
`test_snapshot_create` to verify the snapshot is actually created and appears
in `mngr snapshot list`, instead of only checking the exit code.
