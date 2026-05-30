Fixed the `test_create_with_transfer_git_mirror` e2e release test. It now uses a
60s timeout (the git-mirror full-repo clone exceeds the default 10s function
timeout) and no longer carries an incorrect `@pytest.mark.modal` mark: the test
creates a purely local git-mirror agent and never invokes the `modal` CLI, so
the resource guard's NEVER_INVOKED check was failing once the body passed.
