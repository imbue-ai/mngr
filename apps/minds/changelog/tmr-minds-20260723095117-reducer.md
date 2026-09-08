Automated TMR test-integration run over four minds e2e/release tests. No code changes were integrated: every test agent reported that its test either already passes or was correct as written, so there was nothing to cherry-pick. The run's value is in the environment gaps it surfaced.

`test_claude_code_version_matches_default_workspace_template_pin` passed as-is: the release Dockerfile's `CLAUDE_CODE_VERSION` default still matches default-workspace-template's `[agent_types.claude].version` pin.

Three tests could not be exercised in the agent sandbox and were escalated as environment blockers rather than worked around:

- `test_aws_workspace_runs_in_runsc_container_on_ec2` is double-gated behind real AWS credentials and `MNGR_AWS_RELEASE_TESTS=1` (it provisions and destroys a real EC2 instance); it skipped here.

- `test_latchkey_remote_workspace_gateways_and_state_sync_end_to_end` needs a disposable Linux runner with docker, passwordless sudo, npm, and `MNGR_LATCHKEY_E2E_TESTS=1`; none were present, so it skipped.

- `test_sse_redirect_on_done` requires the Playwright chromium install that `Dockerfile.release.extras` bakes in (`PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright`). The agent confirmed the test itself is correct by reproducing that provisioning locally, after which it passed.
