# Changelog - mngr_lima

A concise, human-friendly summary of changes for the `mngr_lima` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Security

- Security: Lima VMs upgrade `ca-certificates` at provisioning, dropping the compromised `emSign ECC Root CA - G3` root on VMs built from older images.

## [v0.2.0] - 2026-10-22

### Added

- Added: `LimaProviderInstance.rename_host` — updates the logical host name on the (offline-writable) host record, so it works whether or not the VM is running.
- Added: `mngr lima resize HOST [--cpus N] [--memory GIB] [--disk GIB]` changes a host's recorded size. A stopped VM is reconfigured at once; a running one gets the new size at its next `mngr start`, which always brings the VM up to the recorded size. Data disks never shrink.
- Added: Lima hosts report their real size (the VM's CPUs and memory, and the btrfs data disk, or the boot disk on the exposed layout) in `mngr list` (`host.resource`) and discovery (`DiscoveredHost.size`, carried by `mngr observe`), instead of a 4 CPU / 4 GiB / 100 GiB placeholder.
- Added: Create steps `lima.start_vm`, `lima.wait_cloud_init` and `lima.wait_ssh` are reported on mngr's structured progress stream (`--format jsonl`).
- Added: Optional `volume_home_path` config lets the btrfs data disk back a whole home tree (e.g. `/home/user`) with `host_dir` inside it; plain-text service logs honor the new `host_log_dir` config.
- Added: `LIMA_HOST_ACCESS_MANIFEST` (with `LIMA_HOST_KEY_NAME`, `LIMA_ROOT_SSH_KEY_NAME`, `LIMA_KNOWN_HOSTS_NAME`) in `constants.py` publishes the per-VM key layout under `keys/hosts/<host id>/`.
- Added: Discovery heals Lima's persisted agent store (persists missing live-agent records, removes orphaned ones). Host-record reads retry parse failures, then treat the record as missing, or raise `HostRecordUnreadableError` when `strict_host_record_parsing` is enabled.

### Changed

- Changed: New Lima VMs derive their `limactl` instance name from the immutable host id (`<prefix><host_id>`) rather than the host name, so a rename never leaves the VM's instance name out of sync with the host name (`limactl` has no native rename). Existing VMs created under the old `<prefix><host_name>` scheme keep working; discovery reads the host name and instance name from the persisted record rather than parsing the instance name.
- Changed: Lima VM provisioning script's required-package check now includes `flock` (the `util-linux` package), used by mngr's unified cross-actor host lock and the in-host idle-shutdown watcher. Already present on standard Debian images; only installs on minimal/custom images that lack it.
- Changed: When `limactl` is installed and new enough but an invocation fails at runtime, Lima discovery now reports the provider as unavailable (matching how other providers report an unreachable backend, with the same retry/backoff), instead of silently marking every Lima host offline. A genuinely absent or too-old `limactl` still degrades gracefully to a local, all-offline host view.
- Changed: The default guest image is Debian 13 "trixie" genericcloud `20260722-2547` from imbue's artifact mirror (`apt.imbuepackages.com/artifacts/debian-cloud-image/`), replacing Debian 12 from `cloud.debian.org`; `default_image_url_aarch64` / `default_image_url_x86_64` still override it.
- Changed: New run-as-root VMs authorize a per-host root client key minted under the host's keys dir instead of the provider-wide `root_ssh_key` (older VMs fall back to the shared pair); the per-host `known_hosts` is rendered through mngr core's host-key pin store.
- Changed: Provisioning caps the systemd journal at 512 MiB and, in the btrfs layout, grows the data filesystem to fill its disk on every boot.
- Changed: A transient network failure while downloading the base image retries `limactl start` once; 404/403 are not retried.

### Fixed

- Fixed: Creating a Lima VM no longer fails with a `UNIX_PATH_MAX=104 characters` error when the home path and mngr prefix are long. Lima names each VM's SSH socket after the instance name (`<prefix>host-<32-char id>`) and rejects names that push the socket path over the OS limit, which could fire for longer usernames on longer-prefixed environments (e.g. `minds-staging-`). The random id portion is now shortened just enough to keep the socket path within the limit, leaving the name unchanged when it already fits; if the prefix plus `LIMA_HOME` leaves no room at all, creation fails early with a clear message instead of a cryptic limactl fatal error.
- Fixed: Lima workspace-creation failing with `SSH host key error (Host key for 127.0.0.1 does not match.)` after a transient Debian mirror hiccup during `apt-get install`. The VM provisioning script now installs the pre-trusted SSH host key (and applies sshd tuning) before any package fetch — host-key trust no longer depends on the network — and the package install is wrapped in a retry loop; if it still fails, the error surfaces clearly on an SSH-reachable host.
- Fixed: A `limactl` invocation that fails at runtime (e.g. crashes at startup) is now surfaced as `LimaCommandError` instead of leaking an opaque error. Every wrapper helper (`list`, `stop`, `delete`, `disk create`, `disk delete`, `show-ssh`, `shell`, `start`, `--version`) funnels through a single runner that translates the raw process error, so callers catching `LimaCommandError` no longer miss it. `limactl shell` now raises rather than silently returning a non-zero exit code, so a broken VM fails host creation with a clear error instead of proceeding.
- Fixed: Lima hosts record the `host_dir` they were created with, and reads, the in-VM activity watcher and the service log dir use it. A host created under a project-level `[providers.lima].host_dir` was unreadable from other contexts, and restarting it from one silently stopped idle shutdown.
- Fixed: Creating a host under a name already in use fails with `HostNameConflictError` instead of building a second VM; the name is reserved before `limactl start`, so concurrent creates conflict too.
- Fixed: `delete_host` deletes the Lima VM definition, so `mngr gc` no longer strands VMs in `limactl list`.
- Fixed: The btrfs data disk gets the `lima-<disk_name>` label Lima probes for (existing disks are relabeled), so VMs no longer re-run first-time disk setup on every boot and come up systemd-`degraded`.
- Fixed: Provisioning appends to root's `authorized_keys` instead of truncating it, which dropped keys added after creation (e.g. the imbue_cloud owner key) on every VM start.
- Fixed: Guests with OpenSSH >= 9.8 disable sshd `PerSourcePenalties`, which under qemu user-mode networking could leave a VM permanently unreachable after one auth timeout during a slow boot.
- Fixed: limactl's `"Unknown"` (and any unrecognized) status maps to `HostState.UNKNOWN` instead of `CRASHED`, which triggered unattended auto-restarts and GC eligibility.

## [v0.1.10] - 2026-06-18

## [v0.1.9] - 2026-06-16

### Changed

- Changed: `destroy_host` now raises a `CleanupFailedGroup` carrying the classified cleanup failures (instead of returning them, or swallowing errors as warnings) when a resource is left behind, and returns normally otherwise. A resource that was already gone is treated as benign; a resource that could not be destroyed is recorded as a `HOST_RESOURCE_REMAINS` failure (or `OTHER` for a bookkeeping write failure), so `mngr destroy`/`cleanup` can surface it and exit with a cause-specific code. See `specs/cleanup-error-aggregation.md`.

## [v0.1.8] - 2026-06-16

## [v0.1.7] - 2026-06-15

## [v0.1.6] - 2026-06-13

### Added

- Added: `is_run_as_root` config field on the Lima provider — when enabled, mngr runs the agent in the VM as root (uid 0), so a coding agent can `apt install` and write anywhere with no `sudo`. mngr injects a root client key, enables key-based root login, and SSHes in as root. Requires the btrfs additional-disk layout (`is_host_data_volume_exposed=false`); the invalid combination with the 9p bind-mount layout is rejected at config construction.

### Changed

- Changed: Lima provider runs agents directly in the VM again (docker-in-VM mode removed — see Removed). Consistent dependency setup across providers is now achieved by having the project ship idempotent setup scripts that its `Dockerfile` runs (for docker/vps_docker/ovh) and that the Lima host runs directly after the project is synced in. btrfs-based backups continue to work because `host_dir` stays on a btrfs disk and the root agent can snapshot it directly.
- Changed: Offline hosts produced by this provider implement the new `HostFileReadInterface` (via the shared `make_readable_offline_host` helper / `OfflineHostWithVolume`), so a stopped host's files are readable through the same interface as an online host. Volume resolution is lazy on first read, so no per-host probe is added to host discovery.

### Removed

- Removed: Lima provider's `is_host_in_docker` mode entirely, along with all its config fields (`is_host_in_docker`, `container_ssh_port`, `default_image`, `builder`, `docker_install_timeout`, `container_ssh_connect_timeout`, `image_build_timeout_seconds`, `default_container_run_args`, `docker_runtime`, `install_gvisor_runtime`). Configs that still set these now fail to load; existing docker-mode Lima hosts (records with `is_host_in_docker=true`) are no longer startable — destroy and recreate them. The Lima provider no longer depends on `mngr_vps_docker`.

## [v0.1.5] - 2026-06-08

### Added

- Added: Opt-in `is_host_in_docker` mode on the Lima provider (`providers.lima.is_host_in_docker`, default `false`). When enabled, the agent runs inside a Docker container *in* the Lima VM (built from the project's Dockerfile, exactly like the docker/vps_docker providers) instead of directly in the VM; mngr treats the container as the host (ssh and all agent work happen inside it, Lima forwards the container's sshd out to the host's localhost). Forces `is_host_data_volume_exposed=false`; a per-host btrfs subvolume on the additional disk backs the container's `host_dir`, and the `mngr_vps_docker` snapshot helper is installed in the VM so the in-container agent can trigger consistent `btrfs subvolume snapshot` backups. `mngr stop` powers off the whole VM; `start` boots it and relaunches the container; `destroy` removes the VM and disk. Default (`is_host_in_docker=false`) behavior is unchanged.
- Added: `docker_runtime` and `install_gvisor_runtime` options on the Lima provider config (used in `is_host_in_docker` mode). `docker_runtime` (default unset) passes `--runtime=<value>` to the agent container's `docker run` inside the VM; `install_gvisor_runtime` (default `false`) installs and registers gVisor `runsc` with the in-VM Docker daemon via gVisor's official APT repository (idempotent). Installing is independent of enabling — set `docker_runtime = "runsc"` to run the agent container under gVisor.
- Added: `providers.lima.default_container_run_args` (default empty), extra arguments appended to the `docker run` that starts the agent container in `is_host_in_docker` mode — the only config path for injecting inner-container `docker run` flags on Lima. Pairs with `docker_runtime="runsc"` (e.g. `["--workdir=/", "--security-opt=no-new-privileges"]`).
- Added: `discover_hosts` now warns about orphaned Lima VMs — prefix-matched instances that no host record claims (leftovers from an interrupted create) are logged with the manual `limactl delete --force <name>` cleanup command, since mngr can neither manage nor garbage-collect a VM that has no record.

### Changed

- Changed: The Lima VM now installs a pinned Docker Engine version from Docker's official apt repo (the same version remote VPS providers use) instead of Debian's unpinned `docker.io` package, so workspace hosts run an identical, reproducible Docker regardless of provider.
- Changed: Default Lima VM image switched from Ubuntu 24.04 to a pinned Debian 12 "bookworm" genericcloud image (both `aarch64` and `x86_64`). Now that the agent typically runs inside a Docker container in the VM (`is_host_in_docker`), the VM only needs Docker + btrfs + sshd, and this mirrors the OVH provider's Debian 12 base. Override per-arch via `providers.lima.default_image_url_*`.
- Changed: Provisioning now formats and mounts the per-host btrfs data disk in-guest (idempotent; existing snapshot data survives) instead of relying on Lima's guestagent to auto-format it at boot. Minimal cloud images (the new Debian default) ship no `mkfs.btrfs`, which had left the disk unformatted and broke per-host subvolume creation. On later boots Lima's guestagent handles the mount.

### Fixed

- Fixed: Lima host creation now tears down half-built VMs on any failure, so the VM and its btrfs additional disk are always cleaned up (and a failed-host record written) when creation does not complete — including failure types (concurrency-group errors, timeouts, interrupts) that previously escaped cleanup and left an orphaned, untracked VM behind.

## [v0.1.4] - 2026-06-05

## [v0.1.3] - 2026-06-01

### Added

- Added: Opt-in btrfs host-data volume mode for the Lima provider. New `is_host_data_volume_exposed: bool = True` field on `LimaProviderConfig` controls how `host_dir` is backed: `True` keeps today's 9p bind-mount layout; `False` attaches a Lima-managed btrfs additional disk (100GiB default) and symlinks `host_dir` to it, so `host_dir` is snapshottable as a single consistent btrfs filesystem. The layout is locked on the per-host record at create time so stop/start replay it. New `host_data_disk_size` config field (default `"100GiB"`).

## [v0.1.2] - 2026-05-28

### Changed

- Changed: Dropped `ssh-keyscan` from the host-creation flow — each Lima VM now gets a pre-generated ed25519 host keypair injected via the Lima `provision[mode=system]` script, eliminating the TOFU and the `Broken pipe` race during VM bring-up. Per-host keys live under `<provider-dir>/keys/hosts/<host_id>/`; `merge_lima_yaml` extends (rather than replaces) `provision` and `mounts` so mngr's load-bearing entries are preserved.
- Changed: `mngr create --provider lima` help text now shows `--memory=N` / `--disk=N` (plain integers, no `GiB` suffix), matching what `limactl start` expects.
- Changed: Serial-log tailer switched from `tail --follow=name --retry` (GNU-only) to `tail -F` for macOS BSD-tail compatibility.

### Fixed

- Fixed: Lima provider now actually disables guest → host port forwarding — emits two ignore rules (`guestIP: 0.0.0.0` with `guestIPMustBeZero: true` and `guestIP: 127.0.0.1`), and `merge_lima_yaml` locks `portForwards` against user `--file` overrides.
