## AWS provider support: shared VPS-Docker base refactor

- **Parallel-SSH host-record discovery** lifted from `VultrProvider` into `VpsDockerProvider`. Subclasses now implement two small hooks: `_list_provider_vps_hostnames()` and `_fetch_provider_instances()`. The cache scaffolding for instance listings (`_instances_cache` field, `reset_caches` integration) lives in one place.
- **New `_validate_provider_args_for_create` hook** on `VpsDockerProvider` (default no-op), called by `_provision_vps` immediately before `create_instance`. AWS uses this for its pytest-time `auto_shutdown_minutes` guard.
- **`wait_for_instance_active` lifted onto `VpsClientInterface`** as a default method with a `slow_provisioning_warning_threshold_seconds` field for per-provider tuning. AWS / Vultr no longer duplicate the polling loop.
- **`VpsClientInterface.create_instance` `tags` parameter** widened to `Mapping[str, str]` for read-only-friendly call sites.
- **`os_id` removed from the shared interface**: `VpsClientInterface.create_instance` no longer carries the Vultr-specific image-selection int. `VpsHostConfig` / `ParsedVpsBuildOptions` / `VpsDockerProviderConfig` all lose the field. The `--vps-os=` / `--vps-image=` / `--vps-ami=` build args produce a dedicated error pointing at the per-provider config field that replaces them (`default_os_id` / `default_image_name` / `default_ami_id`).
- **New `auto_shutdown_minutes` field** on `VpsDockerProviderConfig`. Cloud-init schedules `shutdown -P +N` when set; on AWS, paired with `InstanceInitiatedShutdownBehavior=terminate`, the instance auto-terminates from the inside.
- `is_for_host_creation` flag removed; replaced with the default-no-op `bootstrap_for_host_creation` hook on `ProviderBackendInterface`. No behavior change for VPS-Docker subclasses.
- README updated and an out-of-place "OS image selection is provider-specific" block removed (it tried to document the dropped `--vps-os=` arg).
