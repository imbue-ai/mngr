Strengthened the `test_create_with_idle_mode_and_timeout` e2e release test to verify the
concrete effects of `mngr create --idle-mode run --idle-timeout 60`: it now asserts via
`mngr list --format json` that the created agent records the expected command, `idle_mode`
of `RUN`, and `idle_timeout_seconds` of `60`, rather than only checking that the create
command succeeded.
