# Changelog - mngr_gcp

A concise, human-friendly summary of changes for the `mngr_gcp` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Bare placement (`isolation=NONE`): the agent runs directly on the GCE VM (no Docker container). The idle agent runs `shutdown -P now` to stop the instance. The `mngr-isolation` instance-metadata marker stamped at create lets discovery resolve a stopped bare host's placement from the cloud API without SSH.

- Added: Offline `host_dir` support for the GCP provider (matching AWS / Azure). A stopped GCE instance's `host_dir` is now readable without resuming via a new GCS state bucket. `mngr gcp prepare` now also creates the bucket (default name `mngr-state-<project_id>`, configurable via `state_bucket_name`); `mngr gcp cleanup` deletes it and refuses to delete a non-empty bucket unless `--force`. New `is_offline_host_dir_enabled` config field (default on).

### Changed

- Changed: GCP's offline store now holds the full host record (config, IP, host keys) instead of a lossy label subset, matching AWS/Azure. The full `VpsHostRecord` JSON is stored in the `mngr-host-state` instance-metadata value and each agent record in a single `mngr-agent-<id>` metadata value, replacing the per-field `mngr-agent-<id>-<name|type|labels>` layout. Exposed through the shared `HostStateStore` interface.

- Changed: Moved mngr host identity (host id and created-at) out of GCE labels into instance metadata, joining the host name and per-agent records already kept there. Only `mngr-provider` remains a label (the server-side `instances.list` discovery filter). **Backward-incompatibility:** a GCE instance created before this change carries its host id / created-at only in labels, so an already-running pre-upgrade host will no longer resolve by id for offline discovery / `mngr start`. Destroy and recreate such hosts.

- Changed: `project_id` config field now defaults to `None` instead of `""`, making "unset" explicit and matching the other optional identifier fields. Resolution behavior is unchanged (ADC fallback).

- Changed: `allowed_ssh_cidrs` is now typed `ScalarStrTuple` (matching AWS) so a higher-precedence config layer that sets it replaces the whole list rather than being flagged as narrowing.

- Changed: Renamed the package to `mngr_vps`; the GCP provider follows shared base classes whose names dropped "Docker" (`VpsProvider`, `VpsHostRecord`, etc.). Import-only.

### Fixed

- Fixed: Renaming a host now re-stamps the `mngr-host-name` instance metadata that offline discovery reads, so a host renamed while running lists under its new name once stopped.

- Fixed: `mngr create` with unresolvable GCP ADC now raises the contract `ProviderUnavailableError` with curated help text pointing at `gcloud auth application-default login` (and the project/ADC setup) instead of the generic "start Docker" guidance.

## [v0.1.2] - 2026-06-18

### Added

- Added: Native GCE stop/start lifecycle (idle-pause + resume) for GCP hosts. `mngr stop` stops the GCE instance (preserving the boot disk so a paused agent costs only disk storage), `mngr start` resumes it (rebinding known_hosts to the fresh external IP). Stopped instances stay discoverable via instance metadata + labels, so `mngr list` and `mngr start <agent>` keep working while TERMINATED. An in-container idle watcher self-stops the instance via a host-side systemd path/service unit.

### Changed

- Changed: GCP's stopped-host offline discovery/resolution, stop/start lifecycle, known_hosts rebinding, and idle-watcher install now come from the shared `OfflineCapableVpsDockerProvider` base; GCP supplies only the GCE-specific hooks. No behavior change.

## [v0.1.1] - 2026-06-16

### Changed

- Changed: `mngr gcp prepare` / `mngr gcp cleanup` group their GCP-specific options under a "Provider" option group, so `--help` and the generated docs list them ahead of the shared common options instead of below them.
- Changed: GCP release-test settings now also disable the `azure` provider (`[providers.azure] is_enabled = false`), mirroring the existing modal/aws/vultr/ovh disables, so `mngr list` inside the GCP lifecycle tests no longer exits non-zero when Azure credentials aren't resolvable.

### Removed

- Removed: Dead `create_snapshot`, `delete_snapshot`, `list_snapshots`, and `list_ssh_keys` stubs (and the now-unused `_boot_disk_source` helper, snapshots compute client, and `FakeSnapshotsClient` test helper) from `GcpVpsClient`, matching the removal of those abstract methods from the shared `VpsClientInterface`.

## [v0.1.0] - 2026-06-16
