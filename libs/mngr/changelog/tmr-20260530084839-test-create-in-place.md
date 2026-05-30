Removed the incorrect `@pytest.mark.modal` from the `test_create_in_place`
e2e release test. The test creates an agent on the local provider
(`mngr create --transfer=none`) and never provisions a Modal host, so it
never invokes the `modal` CLI. The modal resource guard only records an
invocation when the `modal` binary is actually run (e.g. via
`environment_create` while creating a Modal host), so a local-provider test
carrying the mark failed with "Test marked with @pytest.mark.modal but never
invoked modal". The mark is retained on the genuine `--provider modal` tests
in `test_create_modal.py`, which do exercise the CLI.
