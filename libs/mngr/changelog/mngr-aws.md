## AWS provider support: shared layer changes

The `mngr_aws` plugin lands as a new provider backend. The shared `mngr` layer picks up the following supporting changes:

- New `resolve_backend_and_config(provider_name, mngr_ctx)` helper on `mngr/providers/registry.py`. Both `get_provider_instance` and the `mngr create` bootstrap path use it, replacing duplicated "configured-instance vs. bare-backend-name fallback" logic.
- `is_for_host_creation` removed from `ProviderBackendInterface` (Modal-specific flag was being `del`'d by every other backend); replaced with a default-no-op `bootstrap_for_host_creation(name, config, mngr_ctx)` method that Modal overrides and that `mngr create` invokes before `build_provider_instance`.
- `mngr/api/create.py`'s host-creation bootstrap helper is now public as `bootstrap_backend_for_host_creation(provider_name, mngr_ctx)` so other entry points (e.g. `mngr_tmr`'s snapshot path) can trigger the same one-time bootstrap before calling `get_provider_instance`.
- `aws` added to the remote-backend list and `mngr` plugin catalog.
- `mngr create` CLI markdown docs regenerated to include the AWS provider's build-args help.
- `test_cleanup_stop_action_with_real_agent` and `test_list_command_with_running_filter_alias` marked `@pytest.mark.flaky` after observing intermittent 10s-timeout failures on loaded offload sandboxes; pass locally in <3s.
- `_is_transient_ssh_error` (in both `hosts/host.py` and `hosts/outer_host.py`) now treats Python's built-in `TimeoutError` as transient. pyinfra's `read_output_buffers` raises a bare `TimeoutError` when an SSH command's response doesn't arrive within its per-command read timeout (e.g. when the remote sshd is reloaded mid-read during cloud-init bootstrap); the retry loop now picks it up rather than letting it escape host creation.
- `_run_shell_command`, `_get_file`, `_put_file`, and `execute_streaming_command` on `OuterHost` (and `Host._run_shell_command`) now catch the post-retry `TimeoutError` and surface it as a structured `HostConnectionError`. Their inner retry handlers also disconnect on `TimeoutError` so retries rebuild the SSH connection rather than reusing the dead channel.
