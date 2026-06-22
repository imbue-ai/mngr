Fixed the `mngr ls` short-form listing release test (and hardened the shared e2e fixture).

- The e2e fixture now pins `enabled_backends = ["local", "modal", "docker"]` for the subprocess `mngr`, restricting discovery to the backends the test environment can actually reach. Previously every registered provider backend got a default, enabled instance, so credential-requiring cloud backends (e.g. AWS) failed discovery -- the AWS backend deliberately raises `ProviderUnavailableError` when no credentials are configured, which `mngr list`/`mngr ls` surfaces as an error and a non-zero exit (and boto3 credential resolution could hang for tens of seconds first). This kept `mngr ls` (and the other listing tests) from succeeding.

- Removed the bogus `@pytest.mark.modal` from `test_list_short_form`: in a fresh environment the Modal provider raises `ProviderEmptyError` and is skipped before any `modal` CLI invocation, so the mark tripped the resource guard's "marked but never invoked" check. This matches the existing `test_list_active_filter` / `test_list_stopped_filter` reasoning.

- Strengthened `test_list_short_form` to assert that `mngr ls` (the alias for `mngr list`) actually performs a listing -- it now checks for the empty-environment "No agents found" output rather than only asserting a clean exit.
