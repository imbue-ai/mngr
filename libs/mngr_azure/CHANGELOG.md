# Changelog - mngr_azure

A concise, human-friendly summary of changes for the `mngr_azure` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New `azure` provider backend (`mngr_azure`) running mngr agents in Docker containers on Azure VMs. A thin adapter over the shared `mngr_vps_docker` base, like the `aws` / `gcp` / `vultr` providers. Credentials resolve exclusively via Azure's `DefaultAzureCredential` (`az login` session, service principal via `AZURE_*` env vars, or managed identity); the subscription is resolved automatically from config / `AZURE_SUBSCRIPTION_ID` / the `az` CLI's active subscription, so `--provider azure` works with no config after `az login`. Image defaults to Debian 12 (`Debian:debian-12:12-gen2`) at VM size `Standard_B2s`; build args use the `--azure-` prefix (`--azure-region=`, `--azure-vm-size=`, `--azure-spot`).
- Added: `mngr azure prepare` / `mngr azure cleanup` CLI commands. `prepare` registers the `Microsoft.Compute`/`Network`/`Storage` resource providers and creates the mngr-owned resource group + vnet + subnet + NSG (opening tcp/22 and the container SSH port to `allowed_ssh_cidrs`, which defaults to a warned `0.0.0.0/0` like AWS/GCP; set `[]` to opt out, and note SSH is key-only). `cleanup` is the safe inverse — refuses while any mngr-managed VM still exists in the group, idempotent. Both read defaults from `[providers.<name>]` (selected with `--provider`, default `azure`). Output is `--format`-aware with a `created`/`deleted` boolean so callers can tell a first-run create from an idempotent no-op.
- Added: Per-host create uses delete-options cascade — the public IP, NIC, and OS disk are created with `delete_option=Delete`, so `destroy_instance` deletes only the VM and the rest is reaped automatically. Failed-create cleanup deletes the pre-created NIC/IP in a `finally`; Azure's 180s reservation window after a capacity failure is reclaimed via `reclaim_orphaned_network_resources` invoked by `mngr gc` (an age gate that never disturbs an in-flight concurrent create). Presence-only `--azure-spot` opts into Spot capacity (`priority=Spot`, `eviction_policy=Delete`).
- Added: Azure-specific failure typing — `AzureSubscriptionError` (no resolvable subscription), `AzureProviderError` (cleanup refusal), `InvalidAzureIdentifierError` (VM name violating Azure's shape), and a `ProviderUnavailableError` with Azure-specific actionable guidance (set `AZURE_SUBSCRIPTION_ID` / run `az login` / run `mngr azure prepare`).
- Added: `auto_shutdown_seconds` schedules a cloud-init `shutdown -P +N`. Note that Azure has no native delete-after-duration (unlike AWS/GCP) and an OS shutdown leaves the VM "Stopped (not deallocated)", which still bills for compute.
