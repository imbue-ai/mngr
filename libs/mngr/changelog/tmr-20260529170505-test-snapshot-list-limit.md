Fixed the e2e tutorial snapshot tests: the shared modal-agent helper now
specifies `--type command` (the e2e environment configures no default agent
type) and the tests that transfer files during `mngr create` are marked with
`@pytest.mark.rsync`. Also strengthened `test_snapshot_list_limit` to create
multiple snapshots and verify that `--limit` actually truncates the listing.
