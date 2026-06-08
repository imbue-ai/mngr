# Update fd-leak repro scripts for `get_all_provider_instances` return type

`get_all_provider_instances` now returns a `ProviderInstancesResult` (instances plus any unavailable provider names) instead of a bare list. The `scripts/qi/fd_leak/` repro scripts that iterate its result were updated to use `.instances`.
