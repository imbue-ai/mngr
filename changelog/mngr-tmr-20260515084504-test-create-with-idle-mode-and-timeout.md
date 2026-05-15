Strengthened the `test_create_with_idle_mode_and_timeout` e2e test to verify
that `--idle-mode` and `--idle-timeout` actually propagate to the created
agent (via `mngr list --format json`), rather than only checking that the
create command exited 0.
