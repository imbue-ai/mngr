Fixed the `test_create_address_syntax_existing_host` e2e tutorial test so it
actually exercises the `mngr create NAME@HOST` address syntax against a
non-existent host. The test now passes `--type command` (the isolated test
profile has no default agent type, which was previously masking the intended
"host not found" error) and asserts on the concrete `Could not find host:
my-dev-box` error. Removed the superfluous `@pytest.mark.modal` mark, since this
negative-path test never invokes Modal.
