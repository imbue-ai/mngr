## Azure provider

- New `azure` provider backend (`mngr_azure`) that runs agents in Docker containers on Azure Virtual Machines. A thin adapter over the shared `mngr_vps_docker` base, like `mngr_aws` / `mngr_gcp` / `mngr_vultr`.

- Credentials are resolved exclusively via Azure's `DefaultAzureCredential` (an `az login` session, a service principal via `AZURE_*` env vars, or a managed identity) — `[providers.azure]` config has no credential fields, matching the Modal / AWS / GCP convention. Only `subscription_id` is required (a plain identifier; falls back to the `AZURE_SUBSCRIPTION_ID` env var).

- New `mngr azure prepare` CLI command (registered via `register_cli_commands` hookimpl) does the one-time privileged setup: it registers the `Microsoft.Compute` / `Microsoft.Network` / `Microsoft.Storage` resource providers (new subscriptions start unregistered) and creates the mngr-owned resource group + vnet + subnet + NSG (the NSG is attached to the subnet, opening tcp/22 and the container SSH port to `allowed_ssh_cidrs`). Fail-closed: empty `allowed_ssh_cidrs` makes prepare refuse rather than create a wide-open NSG. The hot path in `AzureVpsClient.create_instance` is lookup-only (`resolve_subnet_id`), so `mngr create --provider azure` runs with a restricted role; a missing subnet points the user at `mngr azure prepare`.

- New `mngr azure cleanup` CLI command, the safe inverse of `prepare`: it deletes the mngr-owned resource group (cascading the vnet/subnet/NSG in one `begin_delete`). It refuses (deletes nothing) while any mngr-managed VM still exists in the group, so it cannot strand a running agent, and only deletes a group it owns (tagged `managed-by=mngr` by `prepare`). Idempotent. Backed by new `AzureVpsClient.delete_managed_resource_group()` and `list_mngr_managed_vms()`.

- **Per-host create with delete-options cascade**: each create makes a Standard-SKU static public IP + a NIC bound to the prepared subnet + a VM. The OS disk, NIC, and public IP are all created with `delete_option=Delete`, so `destroy_instance` deletes only the VM and the rest is reaped automatically — no orphaned resources. (Azure API payloads are built with the typed azure-mgmt SDK models, not plain dicts, because the compute SDK does not remap snake_case dict bodies to the ARM wire format.)

- **Failed-create cleanup**: the public IP + NIC are created before the VM, so a VM create that fails (e.g. `SkuNotAvailable` / quota) would orphan them. `create_instance` deletes them in a `finally` when the VM create did not succeed. Azure reserves a NIC for its would-be VM for 180s after a capacity failure, so when the immediate delete is blocked the orphan is reclaimed self-healingly at the start of the *next* create (`_reclaim_orphaned_network_resources` deletes unattached, mngr-tagged NIC/IPs older than the reservation window — an age gate that never disturbs an in-flight concurrent create). The session-end test scanner likewise reclaims orphaned NIC/IPs so a capacity-failed release-test create leaks nothing.

- **SSH keys** are injected inline at VM create (`os_profile.linux_configuration.ssh`); Azure has no per-key resource, so `upload/list/delete_ssh_key` use an in-memory map (like `mngr_gcp`). The shared cloud-init also forwards the key into root's `authorized_keys`, so mngr's root SSH works regardless of the admin user. Cloud-init `custom_data` is base64-encoded as Azure requires.

- **Image**: Ubuntu 24.04 LTS by default (`Canonical:ubuntu-24_04-lts:server`), which runs cloud-init with the Azure datasource so the shared bootstrap works unchanged. Configurable via `image_publisher` / `image_offer` / `image_sku` / `image_version`. **Default VM size** `Standard_B2s` (B-series is the family most likely to have nonzero quota on a fresh pay-as-you-go subscription).

- **Snapshots** are managed-disk snapshots of the VM's OS disk (`create_option=Copy`).

- **Spot capacity opt-in**: presence-only `--azure-spot` build arg flows through `ParsedAzureBuildOptions(ParsedVpsBuildOptions)` and a `_create_vps_instance` override to set `priority=Spot`, `eviction_policy=Delete`, `billing.max_price=-1`. Azure may reclaim on capacity pressure; the host is deleted (not stopped) on eviction, matching AWS spot's terminate-on-reclaim. The shared `VpsClientInterface.create_instance` contract is unchanged — the spot kwarg lives on the Azure-specific client signature.

- **Azure build args use the `--azure-` prefix**: `--azure-region=`, `--azure-vm-size=`, `--azure-spot`. The old `--vps-*` args raise a migration error.

- **Auto-shutdown caveat (Azure-specific)**: `auto_shutdown_minutes` schedules cloud-init `shutdown -P +N`, but an OS shutdown on Azure leaves the VM "Stopped (not deallocated)", which still bills for compute — Azure has no native delete-after-duration like AWS/GCP. This matches the Vultr provider's documented behavior. The real cost backstop for tests is the session-end orphan scanner (below). A future improvement is true self-deletion via a managed identity + a cloud-init systemd timer.

- VMs are tagged `mngr-provider`, `mngr-host-id`, `mngr-created-at`, and `managed-by=mngr`; discovery filters the resource group's VM list by `mngr-provider` (client-side, since Azure has no server-side tag filter on the VM list).

- Release tests are triple-gated by `MNGR_AZURE_RELEASE_TESTS=1`, credential presence, and a resolvable subscription; a Modal-style `pytest_sessionfinish` hook in `conftest.py` scans the resource group for any VM tagged `mngr-pytest-launched` older than 1h at session end, force-deletes leaks, and fails the session. `AzureProvider` refuses to create a VM under pytest without `auto_shutdown_minutes` set so the scanner's TTL is well-defined.
