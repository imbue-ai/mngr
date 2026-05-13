`mngr tmr`: testing agents now publish a single `outputs.tar.gz` archive into
their state directory (`$MNGR_AGENT_STATE_DIR/plugin/test-map-reduce/`),
containing the renamed `test_output/` directory and an optional incremental
`branch.bundle`. The orchestrator polls for the archive via the per-agent
volume API (which works even when the host is offline) and reconstructs the
agent's branch from the bundle, removing the previous rsync + git-pull
finalization step. Reintegrate mode uses the same path. SSH provider, which
does not expose a volume, is no longer supported for testing-agent outputs.
The integrator agent is unchanged.
