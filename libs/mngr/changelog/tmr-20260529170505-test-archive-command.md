Strengthened the `mngr archive` e2e tutorial test (`test_archive_command`). It
now stops the agent before archiving (matching the tutorial sequence, where the
agent is already stopped at that point) and verifies the agent actually receives
the `archived_at` label, rather than only checking the command's exit code. The
previous version ran `mngr archive` against a still-running agent, which is a
no-op (running agents are skipped without `--force`), so it asserted success
without archiving anything.

Added a companion test (`test_archive_command_force`) covering the running-agent
paths: archiving a running agent without `--force` is skipped with a warning, and
`mngr archive --force` stops then archives it (the true equivalent of
`mngr stop --archive`).
