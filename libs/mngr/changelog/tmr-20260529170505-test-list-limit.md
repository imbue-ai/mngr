Removed the superfluous `@pytest.mark.modal` from the `test_list_limit` e2e
tutorial test. In a fresh environment the Modal per-user environment does not
exist yet, so the Modal backend raises `ProviderEmptyError` and `mngr list`
deliberately skips the Modal provider without ever invoking the `modal` CLI.
The resource guard correctly flagged the mark as never-invoked. Also
strengthened the test to assert that `mngr list --limit 10` reports an empty
listing ("No agents found") in the isolated test environment.
