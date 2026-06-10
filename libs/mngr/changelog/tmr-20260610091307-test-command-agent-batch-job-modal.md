Strengthened the `test_command_agent_batch_job_modal` e2e tutorial test to verify
the batch job actually runs, not just that it is registered. The substituted
command now writes its output to a file, and after reconnecting the test reads it
back via `mngr exec` and asserts that both `train` and `evaluate` ran. This
exercises the tutorial's promise that you can "come back and connect to see the
results" (the reconnect auto-starts the host from its snapshot if `--idle-mode run`
stopped it).
