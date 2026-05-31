# Changelog - mngr_vps_docker

A concise, human-friendly summary of changes for the `mngr_vps_docker` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Per-host outer-side btrfs snapshot helper for the new forever-claude-template `host_backup` service. Each vps-docker host gets `/usr/local/sbin/snapshot_helper.sh` + `snapshot_helper.service` (a systemd unit shipped as a bundled resource in `imbue/mngr_vps_docker/resources/`) that watches a per-host docker volume `mngr-snapshot-trigger-<host_id_hex>` for `request.json` files and produces matching `result.json` files describing the outcome of `btrfs subvolume snapshot` / `btrfs subvolume delete` against the per-host subvolume. That docker volume is mounted into the agent container at `/mngr-snapshot/`; the outer's `<btrfs-mount>/snapshots/` directory is bind-mounted read-only into the container at `/mngr-snapshots/` so restic can read the snapshot. Cloud-init installs `inotify-tools` and `jq`. `destroy_host` removes the per-host snapshot-trigger volume.

### Changed

- Changed: The per-host unified docker volume on Vultr / OVH VPSes is now backed by a btrfs subvolume on a loop-mounted btrfs filesystem on the VPS, so the host's agent data is eligible for consistent `btrfs subvolume snapshot -r` snapshots. A new `_prepare_btrfs_on_outer` step (called from `VpsDockerProvider._setup_container_on_vps`) idempotently installs `btrfs-progs`, `fallocate`-allocates `/var/lib/mngr-btrfs.img` (sized to the outer's free space minus `outer_disk_reserved_gb`), `mkfs.btrfs`'s it, loop-mounts it at `/mngr-btrfs`, persists the mount in `/etc/fstab`, and creates a per-host subvolume at `/mngr-btrfs/<host_id_hex>`. The unified docker volume is then created with `--driver=local --opt type=none --opt device=/mngr-btrfs/<host_id_hex> --opt o=bind`, so its on-disk storage is the btrfs subvolume; `host_store.py` reads the bind-source path out of `Options.device` instead of `Mountpoint`. `destroy_host` runs a best-effort `btrfs subvolume delete` before removing the docker volume. Docker keeps default `data-root=/var/lib/docker` + `storage-driver=overlay2` on ext4; only this one volume's storage is on btrfs. New `VpsDockerProviderConfig` fields: `btrfs_mount_path` (default `/mngr-btrfs`), `btrfs_loop_file_path` (default `/var/lib/mngr-btrfs.img`), `outer_disk_reserved_gb` (default 20). **Breaking change:** existing vultr / ovh hosts created on the prior plain-`docker-volume-create` layout cannot be discovered or managed after upgrade; destroy and recreate them.
- Changed: Consolidated the `docker_vps` provider's two-volume layout (per-user state container volume + per-host data volume) into a single per-host Docker volume on the VPS. The unified volume `mngr-host-vol-<host_id_hex>` now holds `host_state.json`, `agents/<agent_id>.json`, and `host_dir/` side by side, mounted at `/mngr-vol` inside the agent container with `/mngr` symlinked to `/mngr-vol/host_dir`. mngr reads and writes metadata directly on the VPS filesystem via the volume's docker mountpoint (discovered with `docker volume inspect`); the dedicated Alpine "state container" and the per-user `docker-state-<user_id>` volume are no longer created or read. **Breaking change:** existing `docker_vps` hosts created before this release cannot be discovered or managed after upgrade; destroy and recreate them.

## [v0.1.2] - 2026-05-28

### Changed

- Changed: Lifted the shared parallel-SSH discovery into `VpsDockerProvider` behind a new `_list_provider_vps_hostnames()` seam method (concrete providers now only contribute the tag listing); `os_id` widened to `int | str` so providers like OVH can carry friendly image names through the build-args parser.
- Changed: `rsync` added to `generate_cloud_init_user_data`'s package list for belt-and-suspenders symmetry on cloud-init backends.
