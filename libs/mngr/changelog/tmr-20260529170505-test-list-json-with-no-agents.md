Removed the superfluous `@pytest.mark.modal` from the `mngr list --format json`
e2e tutorial test (`test_list_json_with_no_agents`). Listing in a fresh
environment with no agents never invokes the Modal CLI -- it only makes an
in-process-SDK gRPC lookup against a Modal environment that does not exist yet,
then skips the provider -- so the resource guard correctly reported the mark as
never satisfied. The test still runs in the release lane (selected by
`@pytest.mark.release`).
