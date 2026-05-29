Fixed the snapshot tutorial e2e tests (`test_snapshot.py`) so the modal agent
setup helper supplies an explicit `--type command` (the source-coded `claude`
default was removed) and so the tests that create an agent are marked
`@pytest.mark.rsync` to satisfy the test resource guard. Also tightened
`test_snapshot_destroy_all_for_agent` to verify snapshots are actually removed.
