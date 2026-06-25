# Changelog - mngr_gcp

A concise, human-friendly summary of changes for the `mngr_gcp` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Bare placement (`isolation=NONE`) for GCP hosts — agents now run directly on the GCE VM (no Docker container), with idle self-stop via `shutdown -P now` (a GCE guest poweroff stops the instance).
- Added: Per-instance `mngr-isolation` metadata stamped at create, so a running bare host is discoverable and reachable with the default provider config (`mngr conn`/`list`/`stop`/`start`/`destroy` no longer need `-S providers.<name>.isolation=NONE`).
- Added: Offline `host_dir` support — a stopped GCE instance's `host_dir` is now readable without starting it (so `mngr event` / `mngr transcript` / `mngr file` work against it), captured operator-side at `mngr stop` and uploaded to a GCS state bucket. Host + agent records still live in instance metadata. `mngr gcp prepare` creates the GCS state bucket (named `mngr-state-<project_id>` by default, configurable via `state_bucket_name`); `mngr gcp cleanup` deletes it, with a new `--force` flag that opts into deleting it even when it still holds offline host state.
- Added: GCP release suite now runs the shared provider release harness's Trip 1 (full lifecycle, container + bare), Trip 2 (idle auto-shutdown), Trip 3 (snapshot-survives-destroy, asserting documented non-portability), and Trip 4 (error classification — `mngr create` with unresolvable ADC surfaces `ProviderUnavailableError` with curated `gcloud auth application-default login` help; `--vps-*` build arg rejected with the migration hint).

### Changed

- Changed: A missing/unresolvable ADC credential or project now raises the shared `ProviderNotAuthorizedError`. In `mngr list` this surfaces as one consistent error line and a non-zero exit.
- Changed: The GCP provider's `project_id` config field now defaults to `None` instead of `""`, making "unset" explicit and matching the other optional identifier fields. Resolution behavior is unchanged (still falls back to the project ADC resolves from the environment).
- Changed: Moved mngr host identity (host id and created-at) out of GCE labels and into instance metadata, joining the host name and per-agent records already kept there. Only `mngr-provider` remains a label. Host id is now stored verbatim and created-at as an ISO-8601 timestamp (no more GCE-charset lowercasing / encoding). Backward-incompatibility: a GCE instance created before this change carries host id / created-at only in labels, so an *already-running* pre-upgrade host will no longer resolve by id for offline discovery / `mngr start`; destroy and recreate such hosts (online hosts reachable over SSH are unaffected).
- Changed: GCP's offline host/agent store now holds the **full** host record (config, IP, host keys) instead of a lossy field subset, via the `mngr-host-state` instance-metadata value and a single `mngr-agent-<id>` metadata value per agent, matching the AWS/Azure behavior. GCP needs no separate object-storage bucket for this (GCE instance metadata is permissive enough).
- Changed: GCP hosts inherit the shared VPS host-setup fix that registers the gVisor (runsc) runtime with `--overlay2=none`, so an agent container's writable layer persists across a `docker stop`/`start` or host reboot instead of being lost to the default per-sandbox overlay.
- Changed: `mngr rename` now re-stamps the `mngr-host-name` instance metadata (the cheap identity tag offline discovery reads), so a host renamed and then stopped lists under its new name (previously stamped only at create).
- Changed: Updated for the `mngr_vps_docker` → `mngr_vps` package and class rename. Import-only.

### Fixed

- Fixed: `start_host` for a bare host no longer fails reading the host record through the Docker volume; it resolves the store through the realizer.

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
