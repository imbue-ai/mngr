# Changelog - mngr_azure

A concise, human-friendly summary of changes for the `mngr_azure` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Bare placement (`isolation=NONE`) — an Azure OS shutdown does not halt compute billing, so the bare agent's idle `shutdown.sh` runs the ARM self-deallocate directly (the same call the container idle watcher uses), keeping the self-deallocate role assignment and skipping the host-side sentinel watcher. Bare release tests added.
- Added: SSH host keys are unique per host — each Azure host gets its own VPS/VM-root and container sshd host keypair at create time.
- Added: Required Azure Blob **state bucket** (private Storage account + container) as the offline store for Azure hosts (replaces the VM tag mirror). A deallocated VM's full host record (config, IP, host keys) plus per-agent records live in the bucket. `mngr azure prepare` creates the account (default name `mngrst<hash>`, overridable via `state_storage_account_name`) and a `mngr-state` container, and grants the operator's own principal the `Storage Blob Data Contributor` role scoped to that account (Azure splits control plane from data plane, so creating the account does not include data-plane blob access). `mngr azure cleanup` deletes it (refuses non-empty unless `--force`).
- Added: Offline `host_dir` on Azure, on by default (`is_offline_host_dir_enabled`). A deallocated VM's `host_dir` is now readable without SSH. Capture is operator-driven at `mngr stop` (no VM managed identity needed). Set `is_offline_host_dir_enabled = false` to disable.
- Added: A running bare Azure host is discoverable with the default provider config — a `mngr-isolation` VM tag stamped at create lets discovery resolve placement from the cloud API without SSH, so operations no longer need `-S providers.<name>.isolation=NONE` at connect time.

### Changed

- Changed: Unauthenticated Azure now raises the shared `ProviderNotAuthorizedError` at construction (still a `ProviderUnavailableError`), with eager credential validation via a management-scope token request — instead of `DefaultAzureCredential` failing lazily on the first real API call.
- Changed: Azure cleanup refusal when VMs still exist now raises the unified `ManagedResourcesExistError` (previously `AzureProviderError`), matching the message used by the other clouds.
- Changed: `allowed_ssh_cidrs` is now typed `ScalarStrTuple` (matching AWS), so a higher-precedence config layer that sets it replaces the whole list rather than being flagged as narrowing; the config key and default are unchanged.
- Changed: Host-side idle-watcher systemd unit renamed from `mngr-azure-idle-watcher` to the shared `mngr-idle-watcher` as the idle-watcher install lifted into the shared `OfflineCapableVpsProvider`.

### Fixed

- Fixed: `mngr start` of a deallocated Azure host now re-mirrors the resumed host record to the Blob state bucket, so offline / `mngr list` reads no longer report a just-resumed Azure VM as STOPPED until the next mirroring write.
- Fixed: `rename_host` now re-stamps the cheap `mngr-host-name` VM tag (read by offline discovery), so a host renamed and then stopped lists under its new name rather than its old one.

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
