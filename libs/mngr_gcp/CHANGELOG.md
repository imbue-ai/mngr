# Changelog - mngr_gcp

A concise, human-friendly summary of changes for the `mngr_gcp` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Bare placement (`isolation=NONE`) — the idle agent runs `shutdown -P now` as the VM's root, which on GCE stops the instance; the container-only sentinel + host-side systemd watcher is skipped. Bare release tests added.
- Added: SSH host keys are unique per host — each GCP host gets its own VPS/VM-root and container sshd host keypair at create time.
- Added: Offline `host_dir` support, matching the AWS / Azure shape. A stopped GCE instance's `host_dir` is now readable without SSH; capture is operator-driven at `mngr stop` to a Google Cloud Storage state bucket. `mngr gcp prepare` creates the bucket (default name `mngr-state-<project_id>`, overridable via `state_bucket_name`); `mngr gcp cleanup` deletes it (`--force` to delete when it still holds offline state).
- Added: `is_offline_host_dir_enabled` config field (default on) to disable offline `host_dir` capture without removing the bucket.
- Added: A running bare GCP host is discoverable with the default provider config — a `mngr-isolation` metadata item stamped at create lets discovery resolve placement from the cloud API without SSH.

### Changed

- Changed: GCP now stores the **full** host record (config, IP, host keys) in instance metadata (`mngr-host-state` + per-agent `mngr-agent-<id>` values), matching AWS / Azure offline behavior. mngr host identity (host id, created-at) moved out of GCE labels into instance metadata; only `mngr-provider` remains a label (the discovery filter). Host id is now stored verbatim and created-at as an ISO-8601 timestamp (no more GCE-charset lowercasing / `%Y-%m-%dt%H-%M-%S` encoding).
- Changed: **Backward incompatibility:** a GCE instance created before this change carries its host id / created-at only in labels, so an *already-running* pre-upgrade host will no longer resolve by id for offline discovery / `mngr start`, and its reconstructed created-at falls back to `now()`. Destroy and recreate such hosts. Online hosts reachable over SSH are unaffected (they resolve via the on-volume records).
- Changed: Unauthenticated GCP now raises the shared `ProviderNotAuthorizedError`; reported consistently with the other cloud providers in `mngr list`.
- Changed: GCP missing-credential help text now points at `gcloud auth application-default login` and the project/ADC setup instead of generic "start Docker" guidance.
- Changed: `project_id` config field now defaults to `None` instead of `""`, matching the other optional identifier fields. Resolution behavior is unchanged: an unset `project_id` still falls back to the project ADC resolves from the environment.
- Changed: GCP hosts inherit the shared VPS host-setup fix that registers the gVisor (runsc) runtime with `--overlay2=none`, so an agent container's writable layer persists across a `docker stop`/`start` or host reboot instead of being lost to the default per-sandbox overlay.
- Changed: Host-side idle-watcher systemd unit renamed from `mngr-gcp-idle-watcher` to the shared `mngr-idle-watcher`.

### Fixed

- Fixed: Renaming a host now re-stamps the `mngr-host-name` instance metadata (the cheap identity tag offline discovery reads), so a host renamed and then stopped lists under its new name rather than its old one.

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
