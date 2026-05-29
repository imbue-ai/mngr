Fixed the `test_create_with_pass_host_env` e2e tutorial test so it actually
provisions a Modal agent: it now pins `--type command` (the isolated test
profile has no default agent type) and is marked `@pytest.mark.rsync` (Modal
create transfers the repo via rsync). The test also now verifies that a
host-level env var passed via `--pass-host-env` actually reaches the remote
host by exec'ing `printenv MODAL_TOKEN_ID` on the agent.
