# Changelog - mngr_lima

A concise, human-friendly summary of changes for the `mngr_lima` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.5] - 2026-06-08

### Added

- Added: Opt-in `is_host_in_docker` mode on the Lima provider (`providers.lima.is_host_in_docker`, default `false`). When enabled, the agent runs inside a Docker container *in* the Lima VM (built from the project's Dockerfile, exactly like the docker/vps_docker providers) instead of directly in the VM; mngr treats the container as the host (ssh and all agent work happen inside it, Lima forwards the container's sshd out to the host's localhost). Forces `is_host_data_volume_exposed=false`; a per-host btrfs subvolume on the additional disk backs the container's `host_dir`, and the `mngr_vps_docker` snapshot helper is installed in the VM so the in-container agent can trigger consistent `btrfs subvolume snapshot` backups. `mngr stop` powers off the whole VM; `start` boots it and relaunches the container; `destroy` removes the VM and disk. Default (`is_host_in_docker=false`) behavior is unchanged.
- Added: `docker_runtime` and `install_gvisor_runtime` options on the Lima provider config (used in `is_host_in_docker` mode). `docker_runtime` (default unset) passes `--runtime=<value>` to the agent container's `docker run` inside the VM; `install_gvisor_runtime` (default `false`) installs and registers gVisor `runsc` with the in-VM Docker daemon via gVisor's official APT repository (idempotent). Installing is independent of enabling — set `docker_runtime = "runsc"` to run the agent container under gVisor.
- Added: `providers.lima.default_container_run_args` (default empty), extra arguments appended to the `docker run` that starts the agent container in `is_host_in_docker` mode — the only config path for injecting inner-container `docker run` flags on Lima. Pairs with `docker_runtime="runsc"` (e.g. `["--workdir=/", "--security-opt=no-new-privileges"]`).
- Added: `discover_hosts` now warns about orphaned Lima VMs — prefix-matched instances that no host record claims (leftovers from an interrupted create) are logged with the manual `limactl delete --force <name>` cleanup command, since mngr can neither manage nor garbage-collect a VM that has no record.

### Changed

- Changed: The Lima VM now installs a pinned Docker Engine version from Docker's official apt repo (the same version remote VPS providers use) instead of Debian's unpinned `docker.io` package, so workspace hosts run an identical, reproducible Docker regardless of provider.
- Changed: Default Lima VM image switched from Ubuntu 24.04 to a pinned Debian 12 "bookworm" genericcloud image (both `aarch64` and `x86_64`). Now that the agent typically runs inside a Docker container in the VM (`is_host_in_docker`), the VM only needs Docker + btrfs + sshd, and this mirrors the OVH provider's Debian 12 base. Override per-arch via `providers.lima.default_image_url_*`.
- Changed: Provisioning now formats and mounts the per-host btrfs data disk in-guest (installing `btrfs-progs`, formatting if not already btrfs — idempotent; existing snapshot data survives — and mounting at the canonical path), instead of relying on Lima's guestagent to auto-format it at boot. Minimal cloud images (the new Debian default) ship no `mkfs.btrfs`, which had left the disk unformatted and broke per-host subvolume creation. On later boots Lima's guestagent handles the mount.

### Fixed

- Fixed: Lima host creation now tears down half-built VMs on any failure. Both `create_host` and the docker-mode `_create_docker_host` use a success-flag + `finally` so the VM and its btrfs additional disk are always cleaned up (and a failed-host record written) when creation does not complete — including failures that are not `MngrError`/`OSError` (e.g. concurrency-group errors, timeouts, or interrupts) which previously escaped the `except` clause and left an orphaned, untracked VM behind. The docker-mode path also drops the container's forwarded-port `known_hosts` entry on cleanup.

## [v0.1.4] - 2026-06-05

## [v0.1.3] - 2026-06-01

### Added

- Added: Opt-in btrfs host-data volume mode for the Lima provider. New `is_host_data_volume_exposed: bool = True` field on `LimaProviderConfig` (and the matching persisted field on `LimaHostConfig`) controls how `host_dir` is backed: `True` keeps today's 9p bind-mount layout; `False` attaches a Lima-managed btrfs `additionalDisk` (`mngr-<host_id_hex>-data`, 100GiB default, qcow2 sparse) and symlinks `host_dir` to Lima's auto-mount path, so `host_dir` is snapshottable as a single consistent btrfs filesystem. `get_volume_for_host()` returns `None` in btrfs mode; the value is locked on the per-host record at create time so `stop_host` / `start_host` replay the same layout. New `host_data_disk_size` config field (default `"100GiB"`) and `limactl_disk_create` / `limactl_disk_delete` helpers. `destroy_host` / `delete_host` remove the named Lima disk when in btrfs mode.

## [v0.1.2] - 2026-05-28

### Changed

- Changed: Dropped `ssh-keyscan` from the host-creation flow — each Lima VM now gets a pre-generated ed25519 host keypair injected via the Lima `provision[mode=system]` script, eliminating the TOFU and the `Broken pipe` race during VM bring-up. Per-host keys live under `<provider-dir>/keys/hosts/<host_id>/`; `merge_lima_yaml` extends (rather than replaces) `provision` and `mounts` so mngr's load-bearing entries are preserved.
- Changed: `mngr create --provider lima` help text now shows `--memory=N` / `--disk=N` (plain integers, no `GiB` suffix), matching what `limactl start` expects.
- Changed: Serial-log tailer switched from `tail --follow=name --retry` (GNU-only) to `tail -F` for macOS BSD-tail compatibility.

### Fixed

- Fixed: Lima provider now actually disables guest → host port forwarding — emits two ignore rules (`guestIP: 0.0.0.0` with `guestIPMustBeZero: true` and `guestIP: 127.0.0.1`), and `merge_lima_yaml` locks `portForwards` against user `--file` overrides.
