## AWS provider support: ProviderBackendInterface refactor

`is_for_host_creation` was removed from `ProviderBackendInterface` (Modal-specific flag was being `del`'d in every other backend). Modal now overrides a default-no-op `bootstrap_for_host_creation(name, config, mngr_ctx)` method on the interface, where the per-user environment registration moves. `mngr create` invokes this hook before `build_provider_instance`. No behavior change for Modal.
