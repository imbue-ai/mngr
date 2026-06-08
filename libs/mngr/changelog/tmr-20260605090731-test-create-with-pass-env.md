Fixed the `test_create_with_pass_env` e2e tutorial test, which was incorrectly
marked `@pytest.mark.modal` despite never exercising the modal provider (it
creates a local `--type command` agent). The modal resource guard failed the
test during teardown; removing the mark fixes it.

Added a companion unhappy-path test, `test_create_with_pass_env_unset`, that
verifies `mngr create --pass-env` silently skips a variable that is not set in
the current shell: the agent is still created and the variable is absent from
its environment.
