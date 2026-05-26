## libs/mngr

- Removed `@pytest.mark.modal` from `test_list_json_with_no_agents` in `libs/mngr/imbue/mngr/e2e/test_list.py`. After the introduction of `ProviderEmptyError`, `mngr list` no longer auto-creates a Modal environment when one does not yet exist, so the empty-env path never invokes the `modal` CLI. The resource guard's `NEVER_INVOKED` check was failing the test as a result.
