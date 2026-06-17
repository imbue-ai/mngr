# Changelog - mngr_azure

A concise, human-friendly summary of changes for the `mngr_azure` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New `azure` provider backend (`mngr_azure`) running mngr agents in Docker containers on Azure VMs. A thin adapter over the shared `mngr_vps_docker` base, like the `aws` / `gcp` / `vultr` providers. Credentials resolve exclusively via Azure's `DefaultAzureCredential` (`az login` session, service principal via `AZURE_*` env vars, or managed identity); the subscription is resolved automatically from config / `AZURE_SUBSCRIPTION_ID` / the `az` CLI's active subscription, so `--provider azure` works with no config after `az login`. Image defaults to Debian 12 (`Debian:debian-12:12-gen2`) at VM size `Standard_B2s`; build args use the `--azure-` prefix (`--azure-region=`, `--azure-vm-size=`, `--azure-spot`).
- Added: `mngr azure prepare` / `mngr azure cleanup` CLI commands. `prepare` registers the `Microsoft.Compute`/`Network`/`Storage` resource providers and creates the mngr-owned resource group + vnet + subnet + NSG (opening tcp/22 and the container SSH port to `allowed_ssh_cidrs`, which defaults to a warned `0.0.0.0/0` like AWS/GCP; set `[]` to opt out, and note SSH is key-only). `cleanup` is the safe inverse — refuses while any mngr-managed VM still exists in the group, idempotent. Both read defaults from `[providers.<name>]` (selected with `--provider`, default `azure`). Output is `--format`-aware with a `created`/`deleted` boolean so callers can tell a first-run create from an idempotent no-op.
- Added: Leak-free resource lifecycle — destroying an agent removes its VM and all associated resources (public IP, NIC, OS disk) together, and a failed create cleans up after itself, including Azure's 180s post-failure reservation window (reclaimed at `mngr gc` time). Spot capacity is opt-in via `--azure-spot`.
- Added: Actionable error messages for Azure misconfiguration — e.g. an unresolvable subscription or credential points you at setting `AZURE_SUBSCRIPTION_ID`, running `az login`, or running `mngr azure prepare`, rather than a generic provider failure.
- Added: `auto_shutdown_seconds` schedules a cloud-init `shutdown -P +N`. Note that Azure has no native delete-after-duration (unlike AWS/GCP) and an OS shutdown leaves the VM "Stopped (not deallocated)", which still bills for compute.
