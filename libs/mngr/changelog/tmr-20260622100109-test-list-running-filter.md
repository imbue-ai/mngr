Hardened the `mngr list --running` e2e tutorial test (`test_list_running_filter`):

- Added an explicit `@pytest.mark.timeout(180)` override, matching the other agent-creating list tests, so the test's five subprocess commands no longer trip the default 10s per-test timeout.

- Scoped the verification listing to the local provider (`mngr list --running --provider local`), where the test's agents actually live. This still exercises the `--running` filter exactly while avoiding an all-provider listing that aborts on a registered-but-unconfigured remote backend (e.g. AWS without credentials, which the monorepo registers via `uv sync --all-packages`).

- Removed the now-stale `@pytest.mark.rsync` mark: rsync was only invoked incidentally by the prior full-discovery listing reaching remote hosts, not by the local agent creation, so the mark no longer reflects what the test exercises.

- Strengthened the assertions to confirm the `--running` filter genuinely discriminates: an unfiltered local listing must still contain both agents, with `stopped-agent` reported in a non-RUNNING (`STOPPED`) state, proving it was filtered out rather than destroyed.
