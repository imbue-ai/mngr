Fixed the `test_tips_exec_env_inspect` e2e tutorial release test so it passes in environments where a provider is enabled but unreachable (e.g. AWS without credentials).

- Scoped the agent-id lookup to `mngr list --ids --provider local`, mirroring the sibling fan-out test. The agent under test is a local `--type command` agent, and an unreachable provider would otherwise make the unscoped `mngr list` exit non-zero.

- Removed the spurious `@pytest.mark.rsync` mark. The test only creates a local command agent and runs `mngr exec`; it never invokes rsync, so the resource guard rejected the mark as "marked with @pytest.mark.rsync but never invoked rsync".
