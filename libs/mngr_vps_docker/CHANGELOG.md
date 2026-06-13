# Changelog - mngr_vps_docker

A concise, human-friendly summary of changes for the `mngr_vps_docker` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: AWS-provider shared-layer refactor — parallel-SSH host-record discovery, `wait_for_instance_active`, and other shared mechanics lifted from `VultrProvider` into `VpsDockerProvider`. New extension hooks for subclasses: `_list_provider_vps_hostnames`, `_fetch_provider_instances`, `_validate_provider_args_for_create`, `_create_vps_instance`. New public `MinimalVpsDockerProvider` (used by `mngr_imbue_cloud`'s slow path; pairs with a `vps_client` whose ordering / snapshot / SSH-key calls raise, so provisioning is managed elsewhere and this provider only runs the post-provisioning host-setup machinery). `VpsDockerProvider._parse_build_args` is now a real `@abstractmethod`.
- Added: `auto_shutdown_minutes` field on `VpsDockerProviderConfig` schedules `shutdown -P +N` via cloud-init when set; combined with AWS's always-on `InstanceInitiatedShutdownBehavior=terminate`, the instance auto-terminates from inside — useful as a runaway-cost safety net for ephemeral / test hosts.
- Added: New composable build-arg parser helpers (`extract_single_value_arg`, `extract_git_depth`, `extract_presence_flag`, `raise_if_vps_migration_arg`, `raise_if_unknown_provider_arg`) underpinning `parse_vps_build_args`. `extract_presence_flag` covers boolean opt-in flags and rejects the value-bearing form (e.g. `--aws-spot=true`) so a likely typo fails fast.

### Changed

- Changed: **Breaking** — per-host build args' shared `--vps-*` prefix is gone; each provider now uses its native prefix (`--aws-region` / `--aws-instance-type`, `--vultr-region` / `--vultr-plan`, `--ovh-datacenter` (alias `--ovh-region`) / `--ovh-plan`). `--git-depth=` stays shared (it's about the local mngr build context). The shared parser is `parse_vps_build_args` (public) with `provider_prefix` + `plan_arg_name`; each provider overrides `_parse_build_args`. `default_plan` is dropped from `VpsDockerProviderConfig` (each provider's config carries its own native field).
- Changed: `vps_boot_timeout` renamed to `instance_boot_timeout` (dropping leaked "VPS" terminology now that hyperscalers (AWS, future GCP/Azure) are in scope).
- Changed: `os_id` removed from the shared interface (`VpsClientInterface.create_instance` no longer carries the Vultr-specific image-selection int; `VpsHostConfig` / `ParsedVpsBuildOptions` / `VpsDockerProviderConfig` all lose the field). The `--vps-os=` / `--vps-image=` / `--vps-ami=` build args produce a dedicated migration error pointing at the per-provider config field that replaces them.
- Changed: **Breaking** — `auto_shutdown_minutes` renamed to `auto_shutdown_seconds` on `VpsDockerProviderConfig`, for unit consistency with the rest of the config (everything else is seconds) and to sit alongside the existing seconds-based `default_idle_timeout`. Cloud-init rounds the value up to whole minutes for `shutdown -P +N` (the granularity `shutdown` accepts), with a floor of 1 minute for any positive value. **Action required:** any `settings.toml` using `auto_shutdown_minutes` must switch to `auto_shutdown_seconds` and multiply by 60.
- Changed: Cloud-init now installs Docker via the Debian `docker.io` package instead of `curl get.docker.com | sh` (the packaged install runs inline with cloud-init's other apt packages and finishes in ~5-15s on a `t3.small`, vs ~60-120s for the upstream installer script).
- Changed: Cloud-init `sshd` MaxSessions / MaxStartups bump uses a drop-in `/etc/ssh/sshd_config.d/99-mngr.conf` plus `systemctl reload ssh` (SIGHUP, no connection drop), instead of an in-place `sshd_config` rewrite + `systemctl restart` (which was tearing down in-flight SSH connections and hanging the provisioning poll loop on pyinfra's 10s per-command read timeout).
- Changed: `_wait_for_cloud_init` swallows transient `HostConnectionError` per poll so the loop survives sshd reload windows; the outer `timeout_seconds` remains the hard wall.
- Changed: `VpsClientInterface.wait_for_instance_active` now logs at debug level (instead of silently `pass`-ing) when an instance reports ACTIVE but has no IP yet and the poll is retried, so a stuck provision is traceable without spamming the happy path.
- Changed: Offline hosts produced by this provider implement the new `HostFileReadInterface` — the offline-host construction path (used by both `get_host` and `to_offline_host`) returns an `OfflineHostWithVolume` via the shared `make_readable_offline_host` helper, so a stopped host's files are readable through the same interface as an online host (used e.g. by Claude session preservation when a host is destroyed while offline). Volume resolution is lazy on first read, so this adds no per-host probe to host discovery.

### Fixed

- Fixed: `builder = "DEPOT"` builds were broken for all VPS backends (aws/vultr/ovh). The depot CLI installs to `$HOME/.depot/bin/depot`, which is not on the non-interactive shell's PATH, but `build_image_on_outer` invoked it by bare name. The CLI is now resolved at run time (preferring an existing `depot` on PATH); a second bug in the same path forwarded `DEPOT_TOKEN` via the streaming SSH command's `env` but env forwarding for compound commands was broken in `mngr` core — both are now fixed.
- Fixed: `builder = "DEPOT"` without `DEPOT_TOKEN` now fails fast, before provisioning. Previously a DEPOT build whose `DEPOT_TOKEN` was unset failed only at the build step — after a billable VPS had already been provisioned and cloud-init had run. `create_host` now runs an `ensure_depot_token_available` preflight up front (only when the create will actually build, i.e. non-empty docker build args), raising the same actionable error before any instance is created.
- Fixed: `mngr create` against VPS Docker backends (aws/vultr/ovh) no longer fails during the post-build git seed with `remote rejected ... refusing to update checked out branch` when the build context is a primary git checkout (`.git` is a directory) that has linked worktrees — e.g. running `mngr create -t aws` from a main checkout that keeps a worktree per branch. The remote `docker build` flow now clones *any* local git context into a temp dir before upload (was: only a linked worktree or an explicit `--git-depth` triggered the clone). The operator's working tree (including uncommitted edits) is still overlaid onto the clone, so in-flight changes continue to reach the build.
- Fixed: `start_container` (shared by vps_docker / ovh / lima) is now resilient to restarting a container under gVisor (runsc). A leftover runsc sandbox from the container's previous run can keep the rootfs-overlay `.gvisor.filestore` mounted, so `docker start` fails with "repeated submounts are not supported with overlay optimizations". `start_container` now runs the start + recovery + retry as a single remote script: on that specific gVisor error it reaps the leftover runsc processes scoped to that container id, removes the stale on-disk filestore, then retries.

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
