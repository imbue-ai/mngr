## AWS provider support: ProviderBackendInterface refactor

`is_for_host_creation` was removed from `ProviderBackendInterface` (Modal-specific flag was being `del`'d in every other backend). Replaced with a default-no-op `bootstrap_for_host_creation(name, config, mngr_ctx)` method on the interface that Modal overrides. The imbue-cloud backend's now-unused `del`-of-`is_for_host_creation` is removed. No behavior change.

`mngr imbue_cloud admin pool create` now passes `--ovh-datacenter=` instead of `--vps-datacenter=` to the inner `mngr create --provider ovh` command. The OVH provider's `--vps-*` build-arg prefix was retired in this branch and now raises a migration error; the call site here is updated to the new per-provider prefix so pool creation continues to work.
