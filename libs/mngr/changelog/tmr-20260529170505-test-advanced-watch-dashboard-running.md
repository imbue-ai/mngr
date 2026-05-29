Fixed the `test_advanced_watch_dashboard_running` e2e tutorial test: removed the
spurious `@pytest.mark.modal` mark. `mngr list --running` in a fresh environment
does not invoke modal (the modal backend raises `ProviderEmptyError` at
construction when its per-user environment does not exist yet, so list skips
modal without any gRPC call), which made the resource guard fail with
"marked with @pytest.mark.modal but never invoked modal". Also extended the test
to create an agent and verify it is discoverable via `mngr list` while the idle
agent is correctly excluded from the `mngr list --running` dashboard view.
