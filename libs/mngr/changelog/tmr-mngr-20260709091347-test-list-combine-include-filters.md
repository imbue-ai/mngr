Tightened the `test_list_combine_include_filters` e2e tutorial test so it verifies exactly its documented scope:

- The combined `--include` filter test now marks `backend-running` active (via `mngr exec ... touch "$MNGR_AGENT_STATE_DIR/active"`, the established e2e pattern) so it is genuinely in the `RUNNING` state, and asserts the combined `team == backend AND state == RUNNING` filter returns exactly `{backend-running}`. Previously the agent sat in `WAITING`, the filter matched nothing, and the weak negative-only assertions let that empty result pass.

- Removed the redundant single-`--include` baseline (single-filter behavior is covered by `test_list_filter_by_label_cel`) and the spurious `@pytest.mark.rsync` mark (the test never invokes rsync, which tripped the resource guard).

Flagged a cross-cutting e2e-fixture gap (FIXME): bare `mngr list` tutorial commands (no `--provider`) probe every enabled backend and abort with `EXIT_CODE_PROVIDER_INACCESSIBLE` when one is unreachable, so they fail in the release/tmr environment (`-m "not docker"`, no docker daemon, no cloud credentials). The fixture should scope enabled backends to what is actually reachable.
