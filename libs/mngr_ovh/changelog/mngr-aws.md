## AWS provider support: shared VPS-Docker base changes

- `is_for_host_creation` flag removed; replaced with the default-no-op `bootstrap_for_host_creation` hook on `ProviderBackendInterface`. The OVH backend's `del`-of-`is_for_host_creation` is removed; no behavior change.
- `get_build_args_help()` no longer carries the stale "OS image is set via default_image_name..." block — that line described the removed `--vps-os=` shared build arg, not current OVH behavior.
- `OvhVpsClient` picks up the shared `wait_for_instance_active` interface change (now a default method on `VpsClientInterface`).
- **Per-host build args renamed**: `--vps-datacenter=` is now `--ovh-datacenter=` (`--ovh-region=` is accepted as an alias). `--vps-plan=` is now `--ovh-plan=`. The old `--vps-*` prefix raises a migration error. `--git-depth=` stays shared.
- `vps_boot_timeout` config field renamed to `instance_boot_timeout` (matches the base-config rename).
- **OVH release-test fix**: the two `TestOvhProviderLifecycle` `mngr create` invocations now pass `--type claude`, matching the Vultr and AWS release tests. Previously they relied on a configured default agent type, which is never present in the isolated test HOME, so the lifecycle tests failed immediately with "No agent type provided" and could not exercise a real OVH VPS create/exec/destroy cycle.
