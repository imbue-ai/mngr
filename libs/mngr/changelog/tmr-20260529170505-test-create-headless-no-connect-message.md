Fixed the `test_create_headless_no_connect_message` e2e tutorial test, which
was failing because it carried a superfluous `@pytest.mark.modal` while only
creating a local command agent (the resource guard fails a test marked `modal`
that never invokes modal). Removed the mark, and strengthened the test to
verify the actual effect of the headless `--no-connect` create: the agent's
command process is confirmed running on the host (without using `mngr list`,
which would trigger remote-provider discovery).
