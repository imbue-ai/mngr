Fixed the snapshot tutorial e2e tests (`test_snapshot.py`): the shared modal-agent
setup helper now passes `--type command -- sleep <n>` (the e2e environment has no
default agent type) and the agent-creating tests are marked `@pytest.mark.rsync`
(creating a modal agent transfers files via rsync). Also strengthened
`test_snapshot_create_named` to verify the named snapshot actually appears in
`mngr snapshot list` output, rather than only checking the create command's exit code.
