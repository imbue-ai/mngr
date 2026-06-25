# Changelog - mngr_gcp

A concise, human-friendly summary of changes for the `mngr_gcp` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Bare placement (`isolation=NONE`) — a GCP agent can now run directly on the GCE VM's OS instead of in a Docker container. The idle agent runs `shutdown -P now` (which on GCE stops the instance), skipping the container-only sentinel + host-side systemd watcher. A running bare host is discoverable with default config (no `-S providers.gcp.isolation=NONE` at connect time) via a `mngr-isolation` metadata value stamped at create.
- Added: Offline `host_dir` support (new `is_offline_host_dir_enabled` provider config field, on by default), matching the AWS / Azure shape. A stopped GCE instance's `host_dir` is now readable without starting it, captured operator-side at `mngr stop` and uploaded to a GCS state bucket. Host + agent records still live in GCE instance metadata (where they fit comfortably and need no prepare step).
- Added: `mngr gcp prepare` now also creates a GCS state bucket (named `mngr-state-<project_id>` by default, configurable via `state_bucket_name`); `mngr gcp cleanup` deletes it, with a new `--force` flag to opt into deleting when it still holds offline host state from hosts no longer present as instances.

### Changed

- Changed: `project_id` config field now defaults to `None` (was `""`), making "unset" explicit and matching the other optional identifier fields. Resolution behavior is unchanged: an unset `project_id` still falls back to the project Application Default Credentials resolves from the environment.
- Changed: mngr host identity (host id, created-at) moved out of GCE *labels* and into instance *metadata*, joining the host name and per-agent records already kept there. Host id is now stored verbatim and created-at as an ISO-8601 timestamp (no more GCE-charset lowercasing / encoded format). **Backward-incompatibility**: a GCE instance created before this change carries its host id / created-at only in labels, so an already-running pre-upgrade host will no longer resolve by id for offline discovery / `mngr start`, and its reconstructed created-at falls back to now(); destroy and recreate such hosts. Online hosts reachable over SSH are unaffected.
- Changed: GCP's offline host/agent store now holds the full host record (config, IP, host keys) instead of a lossy field subset, matching AWS / Azure. The full record is stored in the `mngr-host-state` instance-metadata value and each agent record in a single `mngr-agent-<id>` metadata value, replacing the per-field `mngr-agent-<id>-<name|type|labels>` layout.
- Changed: Curated `ProviderUnavailableError` help for unresolvable GCP credentials now points at `gcloud auth application-default login` and the project / ADC setup, instead of the generic "start Docker" guidance.

### Fixed

- Fixed: Renaming a host now re-stamps the `mngr-host-name` instance metadata, so a host that was renamed and then stopped lists under its new name in offline discovery (previously the metadata was written only at create).

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
