## AWS provider support: shared VPS-Docker base refactor

- Adopts the new `_fetch_provider_instances` hook on `VpsDockerProvider`; the per-class `_list_instances_cached` override is gone (cache scaffolding now lives on the base).
- `VultrVpsClient` carries `os_id` locally (a field on the client) now that the shared `VpsClientInterface.create_instance` no longer accepts it. `--vps-os=` build arg removed; per-host overrides require a separate Vultr provider instance with its own `default_os_id`.
- `get_build_args_help()` no longer carries the stale "OS image is set via default_os_id..." block — that described the removed shared build arg, not current Vultr behavior.
- Picks up the shared `wait_for_instance_active` interface change (now a default method on `VpsClientInterface`).
- `is_for_host_creation` flag removed; the Vultr backend's `del`-of-`is_for_host_creation` is removed. No behavior change.
- **Per-host build args renamed**: `--vps-region=` is now `--vultr-region=`; `--vps-plan=` is now `--vultr-plan=`. The old `--vps-*` prefix raises a migration error. `--git-depth=` stays shared.

- **Vultr release test create timeout raised 300s -> 600s.** `_run_mngr`'s default subprocess timeout was too tight for a slow Vultr provision (provisioning alone can take ~90s; the full create adds cloud-init + Docker build + rsync), causing intermittent spurious `subprocess.TimeoutExpired` failures unrelated to any real defect.
