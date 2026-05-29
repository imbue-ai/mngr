Fixed the MANAGING SNAPSHOTS e2e tutorial tests: the shared modal agent
setup now passes an explicit `--type command` (agent type is required and has
no default in the isolated test profile) and the create-based tests are marked
`@pytest.mark.rsync` to match the modal create path's resource guard.

Strengthened `test_snapshot_list_for_agent` to assert the listing actually
surfaces the host's automatically created "initial" snapshot, and added
`test_snapshot_list_for_nonexistent_agent` covering the unhappy path where the
identifier matches no agent or host.
