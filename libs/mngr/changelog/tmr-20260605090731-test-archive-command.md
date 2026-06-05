Strengthened the `mngr archive` e2e tutorial test (`test_archive_command`) to verify
the agent is actually archived: it now stops the agent first (mirroring the tutorial
narrative), runs `mngr archive my-task`, and asserts the `archived_at` label is set and
the agent appears under `mngr list --archived`. Added an unhappy-path test
(`test_archive_running_agent_is_skipped`) verifying that archiving a running agent
without `--force` is a no-op that warns and leaves the agent un-archived.
