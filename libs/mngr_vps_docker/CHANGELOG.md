# Changelog - mngr_vps_docker

A concise, human-friendly summary of changes for the `mngr_vps_docker` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `ContainerSetupError` (a `MngrError` subclass) and a `translate_outer_concurrency_errors` boundary context manager in `container_setup`. The outer-host build/upload/snapshot-helper helpers run their work inside `ConcurrencyGroup`s, so failures had surfaced as raw `ConcurrencyExceptionGroup` / `ProcessTimeoutError` (neither a `MngrError`) and slipped past provider `except MngrError` cleanup clauses, leaking half-built hosts. These failures are now re-raised as `ContainerSetupError`, so provider create paths catch and clean them up. Wired into `build_image_on_outer_from_build_args` (clone + upload) and `provision_snapshot_helper_on_outer`.

### Changed

- Changed: Consolidated host-level provisioning into a single source of truth — a new `host_setup.py` module defines the ordered, idempotent, config-gated setup steps (pinned Docker install, optional gVisor `runsc` install, sshd `MaxSessions`/`MaxStartups` tuning, base packages, optional qemu purge). `cloud_init.py` renders its first-boot `runcmd` block from those same steps, and a new `apply_host_setup_on_outer()` runs the identical steps over SSH so a host can be re-provisioned consistently after first boot. The SSH host-key injection stays first-boot-only in the cloud-init wrapper so re-provisioning never resets the VPS host key or breaks `known_hosts`.
- Changed: Docker is now pinned to an exact version (29.5.1 on Debian 12) and installed via the official Docker apt repo with `--allow-downgrades`, so provisioning is reproducible and a re-run upgrades/downgrades an old host to match (replacing the unpinned `get.docker.com | sh` install). gVisor `runsc` is pinned to a dated release and downloaded with checksum verification.
- Changed: Extracted the reusable docker / btrfs / snapshot-helper / image-build helpers out of `VpsDockerProvider` into a new `imbue.mngr_vps_docker.container_setup` module with public names (e.g. `run_container`, `provision_snapshot_helper_on_outer`, `prepare_btrfs_on_outer`, `setup_container_ssh`, `build_image_on_outer_from_build_args`). `VpsDockerProvider` now imports them, and `_setup_container_ssh` / `_build_image_on_vps` delegate to the shared functions — the shared toolkit the Lima provider's new docker-in-VM mode builds on. No behavior change for VPS Docker hosts.

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

- Changed: **Breaking** — the per-host unified docker volume on Vultr / OVH VPSes is now backed by a btrfs subvolume on a loop-mounted btrfs filesystem (`/mngr-btrfs/<host_id_hex>` on `/var/lib/mngr-btrfs.img`), enabling consistent `btrfs subvolume snapshot -r` snapshots. `VpsDockerProvider._setup_container_on_vps` now begins with a new `_prepare_btrfs_on_outer` step that installs `btrfs-progs`, `fallocate`-allocates the image (sized to outer free space minus a reservation), `mkfs.btrfs`'s it, loop-mounts at `/mngr-btrfs`, persists in `/etc/fstab`, and creates a per-host subvolume. The docker volume (`mngr-host-vol-<host_id_hex>`) is created with `--driver=local --opt type=none --opt device=/mngr-btrfs/<host_id_hex> --opt o=bind`; `host_store.py` reads the bind-source path from `Options.device`. Docker itself still uses default `data-root=/var/lib/docker` + `storage-driver=overlay2`. New `btrfs_mount_path`, `btrfs_loop_file_path`, and `outer_disk_reserved_gb` `VpsDockerProviderConfig` fields. Existing vultr / ovh hosts on the prior plain-volume layout cannot be discovered or managed after upgrade — destroy and recreate them.
- Changed: **Breaking** — consolidated the docker_vps provider's two-volume layout (per-user state container volume + per-host data volume) into a single per-host docker volume `mngr-host-vol-<host_id_hex>` holding `host_state.json`, `agents/<agent_id>.json`, and `host_dir/` side by side, mounted at `/mngr-vol` with `/mngr` symlinked to `/mngr-vol/host_dir`. mngr now reads and writes metadata directly via the volume's docker mountpoint (discovered via `docker volume inspect`); the dedicated Alpine state container and the per-user `docker-state-<user_id>` volume are no longer created or read. Existing `docker_vps` hosts created before this release cannot be discovered or managed after upgrade — destroy and recreate them.
- Changed: Provider's `get_host_and_agent_details` override now accepts and forwards the new `offline_field_generators` parameter to the base implementation, so offline plugin fields are populated when a host falls back to offline data.

## [v0.1.2] - 2026-05-28

### Changed

- Changed: Lifted the shared parallel-SSH discovery into `VpsDockerProvider` behind a new `_list_provider_vps_hostnames()` seam method (concrete providers now only contribute the tag listing); `os_id` widened to `int | str` so providers like OVH can carry friendly image names through the build-args parser.
- Changed: `rsync` added to `generate_cloud_init_user_data`'s package list for belt-and-suspenders symmetry on cloud-init backends.
