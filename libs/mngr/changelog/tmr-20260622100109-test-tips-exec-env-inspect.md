Fixed the `test_tips_exec_env_inspect` e2e tutorial test (for `mngr exec my-task -- env | sort`):

- The agent-id cross-check now scopes its lookup to the local provider (`mngr list --provider local --ids`) where `my-task` actually lives. An unscoped `mngr list` also probes other enabled providers, and a single unreachable provider (e.g. an unconfigured AWS) makes `mngr list` exit non-zero even after printing the agents it found.

- Removed the superfluous `@pytest.mark.rsync` mark: the test creates a local command agent and never invokes rsync (rsync is exercised by the modal variants of these exec tips tests).
