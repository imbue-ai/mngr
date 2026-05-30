Fixed the e2e test fixture to configure a default `[commands.create] type` so that `mngr create` invocations in the e2e suite that omit `--type` (matching the tutorial blocks) no longer fail with "No agent type provided". This default was previously source-coded as "claude" and was dropped when the agent-type default moved into user config.

Strengthened the `test_create_modal_pass_host_env` release test to verify that `--pass-host-env` actually forwards the variable to the remote host (via `mngr exec ... printenv`), rather than only asserting that `mngr create` exits successfully.
