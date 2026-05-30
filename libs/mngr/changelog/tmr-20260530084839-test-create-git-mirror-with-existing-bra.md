Fixed the release e2e test `test_create_git_mirror_with_existing_branch`. It was
hitting the default 10s `func_only` pytest timeout while running `mngr create` +
`mngr list` + `mngr exec` in sequence, so it now uses `@pytest.mark.timeout(60)`
(matching the sibling `test_create_with_transfer_none`). Also removed the
superfluous `@pytest.mark.modal` mark: this test only creates a local-provider
agent and runs `mngr list`/`mngr exec`, which exercise Modal host discovery via
the in-subprocess Modal Python SDK (gRPC) rather than the `modal` CLI, so the
Modal resource guard never tracks an invocation and the mark triggered a
"marked with @pytest.mark.modal but never invoked modal" failure.
