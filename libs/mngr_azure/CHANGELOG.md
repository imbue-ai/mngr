# Changelog - mngr_azure

A concise, human-friendly summary of changes for the `mngr_azure` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Bare placement (`isolation=NONE`) for Azure hosts — agents now run directly on the VM (no Docker container). Because an Azure OS shutdown does not halt compute billing, the bare agent's idle `shutdown.sh` runs the ARM self-deallocate directly (the same call the container idle watcher uses), keeping the self-deallocate role assignment.
- Added: Per-instance `mngr-isolation` tag stamped at create, so a running bare host is discoverable and reachable with the default provider config.
- Added: Azure release suite now runs the shared provider release harness's Trip 1 (full lifecycle, container + bare), Trip 2 (idle auto-shutdown), Trip 3 (snapshot-survives-destroy, asserting documented non-portability), and Trip 4 (error classification — `mngr create` with no resolvable subscription surfaces `ProviderUnavailableError` with curated `az login` / subscription-setup help; `--vps-*` build arg rejected with the migration hint).

### Changed

- Changed: A missing subscription or unusable credential now raises the shared `ProviderNotAuthorizedError`. Azure previously validated only the subscription id (`DefaultAzureCredential` authenticates lazily), so unauthenticated environments surfaced as a confusing API error on the first real call; the provider now eagerly requests a management-scope token at construction, matching AWS/GCP.
- Changed: `mngr rename` now re-stamps the cheap `mngr-host-name` VM tag that offline discovery reads, so a renamed-then-stopped host lists under its new name (previously stamped only at create).
- Changed: Updated for the `mngr_vps_docker` → `mngr_vps` package and class rename. Import-only.

### Fixed

- Fixed: `mngr start` of a deallocated Azure host now re-mirrors the resumed host record to the external (Blob bucket) store, so offline / `mngr list` reads no longer report a just-resumed VM as STOPPED until the next mirroring write.
- Fixed: `start_host` for a bare host no longer fails reading the host record through the Docker volume; it resolves the store through the realizer.

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
