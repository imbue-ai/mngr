Fixed the e2e release test `test_message_filtered_via_stdin_delivers_to_matching_agents` so it verifies exactly its scope (the delivery half of the filtered-broadcast tutorial block).

- Dropped the out-of-scope `to_succeed()` assertion on the standalone `mngr list --include ... --ids` precondition. That command runs the full provider-discovery path across every enabled backend, and an enabled-but-unconfigured cloud provider (e.g. aws) makes it exit non-zero by design, which is orthogonal to whether the `--include` filter selects the matching agents. The precondition now checks only that the filter emits exactly the two matching local ids on stdout.

- Removed the superfluous `@pytest.mark.rsync` mark: messaging local command agents delivers over tmux and never syncs files to a remote host, so rsync is never invoked and the resource guard flagged the mark as unsatisfied.
