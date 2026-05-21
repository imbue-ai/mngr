## AWS provider support: shared VPS-Docker base changes

- `is_for_host_creation` flag removed; replaced with the default-no-op `bootstrap_for_host_creation` hook on `ProviderBackendInterface`. The OVH backend's `del`-of-`is_for_host_creation` is removed; no behavior change.
- `get_build_args_help()` no longer carries the stale "OS image is set via default_image_name..." block — that line described the removed `--vps-os=` shared build arg, not current OVH behavior.
- `OvhVpsClient` picks up the shared `wait_for_instance_active` interface change (now a default method on `VpsClientInterface`).
