# Changelog - mngr_vps_docker

A concise, human-friendly summary of changes for the `mngr_vps_docker` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `HostRealizer` seam inside the VPS provider — a new `isolation` config knob (`IsolationMode.CONTAINER` | `NONE`) selects between the `DockerRealizer` (default; preserves all original Docker-container behavior exactly) and a new `BareRealizer` (places the agent directly on the VM's OS, reached at `vps_ip:22` as root, with no Docker). Bare placement is gated to providers with a machine stop/start lifecycle: a provider without one rejects `isolation=NONE` at create time (`BareIsolationNotSupportedError`). AWS, GCP, and Azure all enable it.
- Added: SSH host keys are unique per host. Every VPS-backed host (AWS, GCP, Azure, OVH, Vultr, and imbue_cloud slices) gets its own freshly-generated VPS/VM-root and container sshd host keypair at create time, stored under `<key_dir>/host_keys/<host_id>/` (no longer one keypair shared across every host a provider instance created). `mngr create --format json` surfaces them via a new `get_ssh_host_public_keys` provider method, so pool-bake tooling can persist and pin them instead of scanning. Existing hosts created before this change fall back to the legacy provider-global host key.
- Added: Uniform offline host/agent-state store abstraction (`HostStateStore`) for offline-capable providers, with an object-storage implementation (`BucketHostStateStore`) shared by AWS S3 and Azure Blob. The bucket is required infrastructure: a provider whose bucket has not been provisioned raises an actionable error pointing at its `prepare` command, on the create/label write path as well as on offline reads. Storage errors propagate rather than being swallowed; a malformed record raises rather than being silently dropped.
- Added: Offline `host_dir` capability as a select-once `HostDirBackend` strategy — a cloud-agnostic, bucket-backed `BucketHostDirBackend` (operator-driven capture at `mngr stop` plus an offline-read volume) or a no-op `NullHostDirBackend` when the feature is off.

### Changed

- Changed: **Package renamed `mngr_vps_docker` -> `mngr_vps`** (distribution `imbue-mngr-vps-docker` -> `imbue-mngr-vps`). The shape-agnostic classes dropped "Docker" from their names: `VpsDockerProvider` -> `VpsProvider`, `VpsDockerProviderConfig` -> `VpsProviderConfig`, `MinimalVpsDockerProvider` -> `MinimalVpsProvider`, `OfflineCapableVpsDockerProvider` -> `OfflineCapableVpsProvider`, `VpsDockerHostRecord` -> `VpsHostRecord`, `VpsDockerHostStore` -> `VpsHostStore`, and the error base `VpsDockerError` -> `VpsError`. The genuinely Docker-specific `DockerRealizer` and the `container_setup` helpers keep their names.
- Changed: The shared `OfflineCapableVpsProvider` now owns the cloud stop/start lifecycle (`stop_host` pauses the whole instance so a paused agent costs only disk; `start_host` resumes it). Providers supply only the cloud-API hooks (`_pause_cloud_instance` / `_resume_cloud_instance`).
- Changed: Container hosts self-heal sshd. The agent container's PID-1 entrypoint now restarts sshd on every (re)start once mngr has provisioned a host key, so the container is reachable again after a VM reboot or `docker restart` without waiting for `mngr start`.
- Changed: Registered the gVisor (runsc) runtime with `--overlay2=none` so a container's writable layer is written through to the persistent overlay2 layer and survives a `docker stop`/`start` or host reboot. Previously runsc used its default per-sandbox overlay, which was recreated on every start, silently losing the injected sshd host key, the `/mngr` host_dir symlink, and provisioning markers — leaving the container unreachable until mngr re-provisioned it.

### Fixed

- Fixed: Host lock reporting for VPS/docker/bare hosts now derives status from a real flock held-probe rather than the lock file's presence (the lock file now persists after release; the previous mtime-based check would have reported every previously-locked host as permanently locked).
- Fixed: `mngr destroy` of a stopped (deallocated / powered-off) host no longer leaves the underlying cloud instance and its mirrored state behind. The offline destroy path dispatches up front on the instance's own power state and terminates a stopped instance via the same cloud `destroy_instance` primitive the online path uses — failing loudly with a `CleanupFailedGroup` if termination could not complete.
- Fixed: A running bare (`isolation=NONE`) host is now discoverable and reachable with the default provider config — operations no longer need `-S providers.<cloud>.isolation=NONE` at connect time. A `mngr-isolation` instance marker (tag / metadata) stamped at create lets discovery resolve placement from the cloud API.
- Fixed: `start_host` resume now waits for the VM to actually serve mngr's expected sshd host key (mirroring create's host-key wait), not just any sshd handshake. Specifically fixes GCP bare resume failing with "Unable to connect to port 22" due to GCE's startup-script re-running and `systemctl restart ssh`ing mid-boot.
- Fixed: `mngr snapshot delete` on a VPS now actually takes effect — `delete_snapshot` removes the snapshot from the host record (raising `SnapshotNotFoundError` for an unknown id) in addition to removing the docker image. Previously the docker image was removed but `mngr snapshot list` kept showing the deleted snapshot.
- Fixed: `mngr stop` on a bare host no longer hangs for minutes capturing `host_dir`. A bare `host_dir` holds the agent's full working tree (thousands of small files); per-file uploads now run concurrently across a bounded worker pool, turning a minutes-long stop into seconds.
- Fixed: Hardened the post-finalize idle-watcher install — a missing host record at `_on_host_finalized` (which runs only after the record has been made durable) now fails `create_host` (raising `HostCreationError`, whose cleanup tears the VPS back down) instead of logging a WARNING and silently shipping a host that can never auto-stop on idle.

## [v0.1.10] - 2026-06-18

### Added

- Added: `OfflineCapableVpsDockerProvider`, a shared base for cloud providers (AWS/GCP/Azure) whose hosts can be stopped while their disk persists. Consolidates offline discovery and resolution (reconstructing stopped, SSH-unreachable hosts and their agents from the provider's instance listing), the stop/start lifecycle, instance lookup by host-id, SSH known_hosts rebinding, and the self-stopping idle watcher install behind a small set of per-provider hooks. No user-visible behavior change.

## [v0.1.9] - 2026-06-16

### Fixed

- Fixed: `start_host` (the `mngr stop --stop-host` resume path) now restarts the container's sshd after `docker start`. sshd is launched via `docker exec`, not the container entrypoint, so it does not survive a container stop/start (or a host VM reboot that takes the container down, e.g. an AWS instance stop/start) — without restarting it, the resume timed out waiting for container SSH. Latent gap for every VPS-Docker provider; AWS's native instance stop/start surfaced it.
- Fixed: `start_host` now also relaunches the in-container activity watcher on resume and records a fresh `BOOT` activity timestamp. The watcher is a backgrounded process that does not survive a container stop/start, so without relaunching it a resumed host would silently stop auto-stopping on idle (a latent gap for every VPS-Docker provider). Refreshing `BOOT` activity is required alongside the relaunch: otherwise a resumed-but-idle host keeps its pre-stop activity-file mtimes and the watcher re-stops it within one poll — so resuming an idle host would race a near-immediate auto-stop.

## [v0.1.8] - 2026-06-16

### Changed

- Changed: `prepare_btrfs_on_outer` now skips the loopback allocation/format/mount/fstab steps when the btrfs filesystem is already mounted at the configured mount path (e.g. on an OVH-slice's lima data disk), so a host whose btrfs is provided by an already-mounted real disk can reuse the shared vps_docker bake and slow-path rebuild unchanged.

### Fixed

- Fixed: `host_backup` btrfs snapshot helper (`snapshot_helper.sh`, the `OUTER_TRIGGER` mechanism) no longer re-processes a request it has already serviced; the spurious "snapshot path already exists" failure that masked a successful backup is gone. The helper now skips any request whose `request_id` already appears in `result.json`.

## [v0.1.7] - 2026-06-15

### Fixed

- Fixed: Agent discovery on VPS Docker providers (AWS, OVH, Vultr) now reads agents **live** from each host's container instead of from the persisted `agents/*.json` outer store, so agents created *inside* a container (e.g. by an in-container `mngr create`) are visible to `mngr message`, `mngr connect`, and any other command that resolves agents through discovery. Previously such agents only showed up in `mngr list`, so onboarding messages to an in-container chat agent were never delivered. Each host's running state is derived from the same live read, removing a per-host inspect round-trip.

## [v0.1.6] - 2026-06-13

### Added

- Added: `MinimalVpsDockerProvider` (in `mngr_vps_docker.instance`) pairs with a `vps_client` whose provisioning calls raise (e.g. an `ExternallyManagedVpsClient` stub) -- provisioning is managed elsewhere and this provider only runs the post-provisioning host-setup machinery. Its `_parse_build_args` extracts `--git-depth=N` and forwards the rest to docker; the legacy `--vps-*` prefix is rejected with a migration error. Used by `mngr_imbue_cloud`'s slow path.
- Added: New composable parser helpers (`extract_single_value_arg`, `extract_git_depth`, `extract_presence_flag`, `raise_if_vps_migration_arg`, `raise_if_unknown_provider_arg`); `parse_vps_build_args` is public and rebuilt on top of them. `extract_presence_flag` rejects the value-bearing form (e.g. `--aws-spot=true`) so a likely typo fails fast. `VpsDockerProvider._parse_build_args` is now a real `@abstractmethod`.
- Added: `auto_shutdown_seconds` field on `VpsDockerProviderConfig` (seconds-consistent with the rest of the config; was briefly `auto_shutdown_minutes`). Cloud-init rounds up to whole minutes for `shutdown -P +N`, with a floor of 1 minute for any positive value; on AWS, paired with `InstanceInitiatedShutdownBehavior=terminate`, the instance auto-terminates from the inside. Hard max-lifetime cap, distinct from the activity-based idle timeout.
- Added: `_create_vps_instance` and `_validate_provider_args_for_create` hooks on `VpsDockerProvider` (defaults: mirror the previous direct `create_instance` call, and no-op). AWS uses these to thread `ami_id_override` through and to run a pytest-time `auto_shutdown_minutes` guard. `_provision_vps` now takes `parsed: ParsedVpsBuildOptions` instead of pre-extracted `region` / `plan`.

### Changed

- Changed: Offline hosts produced by this provider implement the new `HostFileReadInterface` — the offline-host construction path (used by both `get_host` and `to_offline_host`) returns an `OfflineHostWithVolume` via the shared `make_readable_offline_host` helper, so a stopped host's files are readable through the same interface as an online host (used e.g. by Claude session preservation when a host is destroyed while offline). Volume resolution is lazy on first read, so this adds no per-host probe to host discovery.
- Changed: Parallel-SSH host-record discovery lifted from `VultrProvider` into `VpsDockerProvider`. Subclasses implement `_list_provider_vps_hostnames()` and `_fetch_provider_instances()`; the cache scaffolding for instance listings now lives on the base.
- Changed: `wait_for_instance_active` lifted onto `VpsClientInterface` as a default method with a `slow_provisioning_warning_threshold_seconds` field for per-provider tuning. AWS / Vultr no longer duplicate the polling loop.
- Changed: `VpsClientInterface.create_instance` `tags` parameter widened to `Mapping[str, str]`; `os_id` removed from the shared interface (each concrete client carries it locally if needed). `--vps-os=` / `--vps-image=` / `--vps-ami=` build args produce a dedicated error pointing at the per-provider config field that replaces them (`default_os_id` / `default_image_name` / `default_ami_id`).
- Changed: Build-args prefix moved per-provider -- `--vps-region=` / `--vps-plan=` are gone, replaced with each provider's native prefix (`--aws-region=` / `--aws-instance-type=`, `--vultr-region=` / `--vultr-plan=`, `--ovh-datacenter=` alias `--ovh-region=` / `--ovh-plan=`). The dropped `--vps-*` prefix raises a migration error. `--git-depth=` stays shared. `default_plan` dropped from `VpsDockerProviderConfig` (providers carry their native field), and `vps_boot_timeout` renamed to `instance_boot_timeout`.
- Changed: `_wait_for_cloud_init` swallows transient `HostConnectionError` per poll so the loop survives windows where sshd is briefly unavailable (e.g. the sshd restart in the host-setup tuning step); the outer `timeout_seconds` remains the hard wall.
- Changed: `builder=DEPOT` without `DEPOT_TOKEN` now fails fast (`ensure_depot_token_available(...)` preflight at `create_host`) before any billable VPS is provisioned. Only runs when the create will actually build (non-empty docker build args); plain image pulls need no token.
- Changed: `is_for_host_creation` flag removed from `ProviderBackendInterface`; replaced with the default-no-op `bootstrap_for_host_creation` hook. No behavior change for VPS-Docker subclasses.

### Fixed

- Fixed: `builder = "DEPOT"` builds, which were broken for all VPS backends (aws/vultr/ovh). The depot CLI installs to `$HOME/.depot/bin/depot` (not on the non-interactive shell's PATH), but `build_image_on_outer` invoked it by bare name (`depot build ...`), failing with `bash: line 1: depot: command not found`. The CLI is now resolved at run time: a `depot` already on PATH is preferred, otherwise it falls back to the installer's off-PATH default `$HOME/.depot/bin/depot`. The same resolved path drives both the idempotent install check and the `depot build` invocation.
- Fixed: `mngr create` against the VPS Docker backends (aws/vultr/ovh) no longer fails the post-build git seed with `remote rejected ... refusing to update checked out branch` when the build context is a primary git checkout (`.git` is a directory) with linked worktrees. The remote-`docker build` flow now clones any local git context into a temp dir before upload; the fresh clone's `.git` is self-contained and carries no `.git/worktrees/` admin, so the operator's other branches are no longer baked into the image as "checked out". The operator's uncommitted edits are still overlaid onto the clone.
- Fixed: vps-docker backups now capture data on every cycle instead of only the first. The outer-side btrfs snapshot helper (`snapshot_helper.sh`) creates each snapshot at a unique caller-named path (`snapshots/<name>`) and the inner `host_backup` service garbage-collects old snapshots; previously the helper reused a single fixed path, which under gVisor (runsc) made every snapshot read after the first delete+recreate come back empty (the gofer cached a handle to the first subvolume), so restic backed up nothing.

## [v0.1.5] - 2026-06-08

### Added

- Added: `ContainerSetupError` (a `MngrError` subclass) now wraps failures in the outer-host build/upload/snapshot-helper steps, which had previously surfaced as non-`MngrError` concurrency-group/timeout errors and slipped past provider cleanup clauses, leaking half-built hosts. Provider create paths now catch and clean them up.

### Changed

- Changed: Consolidated host-level provisioning into a single source of truth — a new `host_setup.py` module defines the ordered, idempotent, config-gated setup steps (pinned Docker install, optional gVisor `runsc` install, sshd `MaxSessions`/`MaxStartups` tuning, base packages, optional qemu purge). `cloud_init.py` renders its first-boot `runcmd` block from those same steps, and a new `apply_host_setup_on_outer()` runs the identical steps over SSH so a host can be re-provisioned consistently after first boot. The SSH host-key injection stays first-boot-only in the cloud-init wrapper so re-provisioning never resets the VPS host key or breaks `known_hosts`.
- Changed: Docker is now pinned to an exact version (29.5.1 on Debian 12) and installed via the official Docker apt repo with `--allow-downgrades`, so provisioning is reproducible and a re-run upgrades/downgrades an old host to match (replacing the unpinned `get.docker.com | sh` install). gVisor `runsc` is pinned to a dated release and downloaded with checksum verification.
- Changed: Extracted the reusable docker / btrfs / snapshot-helper / image-build helpers out of `VpsDockerProvider` into a new shared `container_setup` module — the toolkit the Lima provider's docker-in-VM mode builds on. No behavior change for VPS Docker hosts.

### Fixed

- Fixed: `start_container` (shared by vps_docker / ovh / lima) is now resilient to restarting a container under gVisor (runsc). A leftover runsc sandbox from the container's previous run can keep the rootfs-overlay `.gvisor.filestore` mounted, so `docker start` fails with "repeated submounts are not supported with overlay optimizations". `start_container` now runs the start + recovery + retry as a single remote script: on that specific gVisor error it reaps the leftover runsc processes scoped to that container id, removes the stale on-disk filestore, then retries. A normal start stays a single `docker start`.
- Fixed: The docker-on-VPS/lima build-context upload now passes the SSH port (`-p <port>`) to rsync's ssh transport. Previously `build_ssh_transport_for_outer` dropped the port, so uploads always targeted port 22 — fine for VPS but broken for lima docker-mode, where the VM's sshd is reached via a Lima-forwarded port on 127.0.0.1, causing "No ED25519 host key is known for 127.0.0.1" / host key verification failures.

## [v0.1.4] - 2026-06-05

### Added

- Added: `teardown_container_on_existing_vps` removes a host's container, per-host btrfs subvolume, and named volumes on an already-reachable VPS (no VPS-API calls), for rebuilding a container in place.
- Added: `ExternallyManagedVpsClient`, a `VpsClientInterface` stub for providers that operate on a VPS they did not order (e.g. an imbue_cloud-leased pool host); every ordering / snapshot / SSH-key call raises so a wrong call site fails loudly.

### Changed

- Changed: Refactored `VpsDockerProvider.create_host` so the post-ordering work (container build/run, SSH setup, certified-data + host-record finalize) lives in a single public method, `create_host_on_existing_vps`, that operates over a caller-supplied outer SSH connection and makes no VPS-API ordering calls. `create_host` now orders the VPS and then calls it, so there is exactly one "set up the host after the VPS exists" code path. Consumed by `mngr_imbue_cloud`'s new slow path; existing OVH/Vultr behavior is unchanged.

## [v0.1.3] - 2026-06-01

### Added

- Added: Per-host outer-side btrfs snapshot helper for the new forever-claude-template `host_backup` service. Each vps-docker host now ships `/usr/local/sbin/snapshot_helper.sh` and a `snapshot_helper.service` systemd unit (bundled in `imbue/mngr_vps_docker/resources/`) that watches a per-host docker volume `mngr-snapshot-trigger-<host_id_hex>` for `request.json` files and produces matching `result.json` files describing `btrfs subvolume snapshot` / `btrfs subvolume delete` outcomes. The trigger volume is mounted into the agent container at `/mngr-snapshot/`, and the outer's `<btrfs-mount>/snapshots/` is bind-mounted read-only at `/mngr-snapshots/` so restic can read produced snapshots. Cloud-init now installs `inotify-tools` and `jq`. `destroy_host` removes the per-host snapshot-trigger volume.

### Changed

- Changed: **Breaking** — the per-host unified docker volume on Vultr / OVH VPSes is now backed by a btrfs subvolume on a loop-mounted btrfs filesystem (`/mngr-btrfs/<host_id_hex>`), enabling consistent `btrfs subvolume snapshot -r` snapshots. Provisioning now installs `btrfs-progs`, allocates and formats the image (sized to outer free space minus a reservation), loop-mounts it, and creates a per-host subvolume that backs the docker volume. New `btrfs_mount_path`, `btrfs_loop_file_path`, and `outer_disk_reserved_gb` config fields. Existing vultr / ovh hosts on the prior plain-volume layout cannot be discovered or managed after upgrade — destroy and recreate them.
- Changed: **Breaking** — consolidated the docker_vps provider's two-volume layout (per-user state container volume + per-host data volume) into a single per-host docker volume holding host state, agent records, and `host_dir/` side by side. mngr now reads and writes metadata directly via the volume's docker mountpoint; the dedicated Alpine state container and per-user state volume are no longer created or read. Existing `docker_vps` hosts created before this release cannot be discovered or managed after upgrade — destroy and recreate them.
- Changed: Provider's `get_host_and_agent_details` override now accepts and forwards the new `offline_field_generators` parameter to the base implementation, so offline plugin fields are populated when a host falls back to offline data.

## [v0.1.2] - 2026-05-28

### Changed

- Changed: Lifted the shared parallel-SSH discovery into `VpsDockerProvider` behind a new `_list_provider_vps_hostnames()` seam method (concrete providers now only contribute the tag listing); `os_id` widened to `int | str` so providers like OVH can carry friendly image names through the build-args parser.
- Changed: `rsync` added to `generate_cloud_init_user_data`'s package list for belt-and-suspenders symmetry on cloud-init backends.
