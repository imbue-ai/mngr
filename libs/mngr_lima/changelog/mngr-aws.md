## AWS provider support: ProviderBackendInterface refactor

`is_for_host_creation` was removed from `ProviderBackendInterface` (Modal-specific flag was being `del`'d in every other backend). Replaced with a default-no-op `bootstrap_for_host_creation(name, config, mngr_ctx)` method on the interface that Modal overrides. The Lima backend's now-unused `del`-of-`is_for_host_creation` is removed. No behavior change.
