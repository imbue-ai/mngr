# Changelog - mngr_azure

A concise, human-friendly summary of changes for the `mngr_azure` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Required Azure Blob **state bucket** (a private Storage account + container) as the offline store for Azure hosts. A deallocated VM's full host record (config, IP, host keys) and per-agent records now live in the bucket instead of `mngr-agent-<id>-*` VM tags, removing the tag mirror's silent 256-char `labels` drop. `mngr azure prepare` creates the account + container (account name defaults to `mngrst<hash>`, override with `state_storage_account_name`) and grants the operator's own principal the `Storage Blob Data Contributor` role scoped to the state account, so offline reads/writes work. The bucket is required (no VM-tag fallback) and mngr raises an actionable error pointing at `mngr azure prepare` when it is absent. `mngr azure cleanup` deletes the account and refuses a non-empty one unless `--force` is passed.

- Added: Offline `host_dir`, on by default (new `is_offline_host_dir_enabled` provider config field). At `mngr stop`, the operator-driven capture uploads the VM's `host_dir` to the bucket with the operator's own credentials so `mngr event` / `mngr transcript` work against a stopped agent. A VM that idle-self-deallocates (or crashes) is not captured.

### Removed

- Removed: `AzureVpsClient.add_tags` / `AzureVpsClient.remove_tags` (and their unit tests). They only ever existed to push per-agent records into VM tags for the old tag mirror, which the state bucket replaces.

## [v0.1.1] - 2026-06-18

### Added

- Added: VM-level stop/start lifecycle for Azure hosts. `mngr stop` now **deallocates** the VM (halting compute billing, unlike an OS-level shutdown), preserving the OS disk so a paused agent costs only disk storage; `mngr start` re-allocates it. A deallocated VM stays discoverable via VM tags, so `mngr list` and `mngr start <agent>` keep working. The public IP is static, so SSH host keys survive the stop with no known_hosts rebind.
- Added: Idle self-deallocate via a system-assigned managed identity — the in-VM idle watcher calls the ARM `deallocate` API (via its IMDS token) when idle, achieving true cost parity with AWS/GCP. `mngr azure prepare` creates a least-privilege custom role (`mngr-self-deallocate`), and each VM gets a role assignment scoped to itself at create time. Degrades gracefully (with a clear warning) when the operator lacks role-assignment privilege.
- Added: New `azure-mgmt-authorization` dependency.

### Changed

- Changed: `deallocate_instance` / `start_instance` now honor their `timeout_seconds` and raise `VpsProvisioningError` on deadline expiry, matching the AWS/GCP clients.
- Changed: Azure's stopped-host offline discovery/resolution, deallocate/start lifecycle, and idle-watcher install now come from the shared `OfflineCapableVpsDockerProvider` base; Azure supplies only its specifics as hooks. No behavior change.

## [v0.1.0] - 2026-06-16

### Added

- Added: New `azure` provider backend (`mngr_azure`) — runs mngr agents in Docker containers on Azure VMs, a thin adapter over the shared VPS-Docker base like the `aws` / `gcp` / `vultr` providers. Works with no config after `az login` (credentials via `DefaultAzureCredential`, subscription auto-resolved from config / `AZURE_SUBSCRIPTION_ID` / the active `az` subscription). Defaults to Debian 12 on `Standard_B2s`; Azure build args take the `--azure-` prefix (`--azure-region=`, `--azure-vm-size=`, and `--azure-spot` for opt-in Spot capacity).
- Added: `mngr azure prepare` / `mngr azure cleanup` — one-time network setup (the mngr-owned resource group + vnet + subnet + NSG) and its safe, idempotent inverse, which refuses while any managed VM still exists. `prepare` opens SSH to `allowed_ssh_cidrs`, which defaults to a warned-open `0.0.0.0/0` like AWS/GCP (set `[]` to opt out; auth is key-only).
- Added: Leak-free resource handling — destroying an agent removes its VM and all associated resources together, and failed creates clean up after themselves; misconfiguration surfaces actionable guidance (set `AZURE_SUBSCRIPTION_ID`, run `az login`, or run `mngr azure prepare`) instead of a generic failure.
- Added: `auto_shutdown_seconds` schedules an OS shutdown, but note Azure has no native delete-after-duration (unlike AWS/GCP) and still bills for a stopped-but-not-deallocated VM.
