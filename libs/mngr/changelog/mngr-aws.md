## AWS provider support: shared layer changes

The `mngr_aws` plugin lands as a new provider backend. The shared `mngr` layer picks up the following supporting changes:

- New `resolve_backend_and_config(provider_name, mngr_ctx)` helper on `mngr/providers/registry.py`. Both `get_provider_instance` and the `mngr create` bootstrap path use it, replacing duplicated "configured-instance vs. bare-backend-name fallback" logic.
- `is_for_host_creation` removed from `ProviderBackendInterface` (Modal-specific flag was being `del`'d by every other backend); replaced with a default-no-op `bootstrap_for_host_creation(name, config, mngr_ctx)` method that Modal overrides and that `mngr create` invokes before `build_provider_instance`.
- `aws` added to the remote-backend list and `mngr` plugin catalog.
- `mngr create` CLI markdown docs regenerated to include the AWS provider's build-args help.
- `test_cleanup_stop_action_with_real_agent` and `test_list_command_with_running_filter_alias` marked `@pytest.mark.flaky` after observing intermittent 10s-timeout failures on loaded offload sandboxes; pass locally in <3s.
