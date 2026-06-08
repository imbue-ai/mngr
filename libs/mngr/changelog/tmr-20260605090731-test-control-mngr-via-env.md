Fixed and expanded the e2e release test for controlling mngr via `MNGR__*`
environment variables (`test_control_mngr_via_env`). The test now opts into
assign-by-default behavior so it works around the e2e fixture's
`[commands.create]` local setting, asserts the agent actually lands on the
`local` provider chosen via the env var, and adds an unhappy-path test verifying
that an invalid provider value supplied via the env var is rejected. No
user-facing behavior change.
