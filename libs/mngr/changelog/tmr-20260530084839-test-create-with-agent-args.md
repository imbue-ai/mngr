Removed the unsatisfiable `@pytest.mark.modal` from the e2e release test
`test_create_with_agent_args`. The test creates a local agent and only runs
`mngr list`, which is a read-only discovery path that never invokes the `modal`
CLI (`environment_create` is only reached on the create-host path). Because the
modal resource guard can only observe the `modal` CLI binary across the mngr
subprocess boundary -- not in-process SDK gRPC calls -- the mark could never be
satisfied and the guard failed the test with "marked with @pytest.mark.modal but
never invoked modal".

Also added `test_create_agent_args_require_separator`, an unhappy-path companion
for the same tutorial block: it confirms that without the `--` separator an
agent-style flag (`--model opus`) is parsed as an mngr flag, rejected with a "No
such option" error (exit code 2), and creates no agent.
