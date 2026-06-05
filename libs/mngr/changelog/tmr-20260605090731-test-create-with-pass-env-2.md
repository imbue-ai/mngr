Fixed the `test_create_with_pass_env` e2e tutorial test, which was failing
because it carried a superfluous `@pytest.mark.modal` mark while only creating
a local command agent (the tutorial block uses the default local provider, so
the test never invoked Modal). Removed the unused mark, then strengthened the
test to verify that the forwarded `--pass-env` variable is actually present in
the created agent's environment via `mngr exec`.
