Test-only changes to the Modal create e2e suite.

The shared `e2e` fixture now configures a default agent type
(`[commands.create] type = "claude"`) in the test profile's
`settings.local.toml`. The Modal tutorial commands (e.g.
`mngr create my-task --provider modal`) intentionally omit `--type`,
relying on the user having configured a default -- exactly as the tutorial
documents under `[commands.create] type`. Without this, every Modal create
test failed at argument resolution with "No agent type provided". Non-Modal
create tests pass `--type command` explicitly, so they override this default
and are unaffected.

`test_create_modal_idle_timeout` now verifies the actual effect of
`--idle-timeout 60` instead of only asserting exit code 0: after creating the
agent it runs `mngr list --provider modal --format json` and asserts the
created agent's host reports `idle_timeout_seconds == 60`. The assertion
tolerates the snapshot-backed host that Modal lists alongside the running
host (which reports `None` while unauthenticated).
