Fixed the `test_list_fields_original_branch_with_agent` e2e tutorial test for the WORKING WITH GIT section:

- Scoped its `mngr list` verification to `--provider local` (matching the convention of the other local-agent listing tests). The bare command queries every enabled backend, and in the isolated e2e environment the AWS backend is enabled without credentials, so it reported as unreachable and made `mngr list` exit non-zero even though the agent row rendered correctly.

- Removed the inapplicable `@pytest.mark.rsync` marker: the test only creates a local agent with `--no-connect` and runs `mngr list`, neither of which invokes rsync, so the resource guard failed the otherwise-passing test.
