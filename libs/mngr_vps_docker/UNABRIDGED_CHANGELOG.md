# Unabridged Changelog - mngr_vps_docker

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_vps_docker/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-08

Consolidated host-level provisioning into a single source of truth. A new
`host_setup.py` module defines the ordered, idempotent, config-gated setup steps
(pinned Docker install, optional gVisor `runsc` install, sshd `MaxSessions` /
`MaxStartups` tuning, base packages, and an optional qemu purge). `cloud_init.py`
now renders its first-boot `runcmd` block from those same steps, and a new
`apply_host_setup_on_outer()` runs the identical steps over SSH so a host can be
re-provisioned consistently after first boot.

Docker is now pinned to an exact version (29.5.1 on Debian 12) and installed via
the official Docker apt repo with `--allow-downgrades`, so provisioning is
reproducible and a re-run upgrades/downgrades an old host to match (replacing the
unpinned `get.docker.com | sh` install). gVisor `runsc` is pinned to a dated
release and downloaded + checksum-verified directly.

The SSH host-key injection stays first-boot-only in the cloud-init wrapper and is
deliberately excluded from the re-runnable steps, so re-provisioning never resets
the VPS host key or breaks `known_hosts`.

Made `start_container` (shared by vps_docker / ovh / lima) resilient to restarting
a container under gVisor (runsc). A leftover runsc sandbox from the container's
previous run can keep the rootfs-overlay `.gvisor.filestore` mounted, so
`docker start` fails with "repeated submounts are not supported with overlay
optimizations". `start_container` now runs the start + recovery + retry as a
single remote script: on that specific gVisor error it reaps the leftover runsc
processes scoped to that container id, removes the stale on-disk filestore, then
retries. A normal start stays a single `docker start`.

Fixed the docker-on-VPS/lima build-context upload to pass the SSH port (`-p <port>`) to rsync's ssh transport. Previously `build_ssh_transport_for_outer` dropped the port, so uploads always targeted port 22 -- fine for VPS (sshd on 22) but broken for lima docker-mode, where the VM's sshd is reached via a Lima-forwarded port on 127.0.0.1, causing "No ED25519 host key is known for 127.0.0.1" / host key verification failures.

Added `ContainerSetupError` (a `MngrError` subclass) and a `translate_outer_concurrency_errors` boundary context manager in `container_setup`. The outer-host build/upload/snapshot-helper helpers run their work inside `ConcurrencyGroup`s, so failures surfaced as raw `ConcurrencyExceptionGroup` / `ProcessTimeoutError` -- neither a `MngrError` -- and slipped past provider `except MngrError` cleanup clauses, leaking half-built hosts. These failures are now re-raised as `ContainerSetupError` (preserving the cause), so provider create paths catch and clean them up. Wired into `build_image_on_outer_from_build_args` (clone + upload) and `provision_snapshot_helper_on_outer`.

Extracted the reusable docker/btrfs/snapshot-helper/image-build helpers out of
`VpsDockerProvider` into a new `imbue.mngr_vps_docker.container_setup` module
with public names (e.g. `run_container`, `provision_snapshot_helper_on_outer`,
`prepare_btrfs_on_outer`, `setup_container_ssh`,
`build_image_on_outer_from_build_args`). `VpsDockerProvider` now imports them,
and the `_setup_container_ssh` / `_build_image_on_vps` methods delegate to the
shared functions. No behavior change for VPS Docker hosts; this is the shared
toolkit the Lima provider's new docker-in-VM mode builds on.

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-06-03

Refactored `VpsDockerProvider.create_host` so the post-ordering work (container
build/run, SSH setup, certified-data + host-record finalize) lives in a single
public method, `create_host_on_existing_vps`, that operates over a caller-supplied
outer SSH connection and makes no VPS-API (ordering) calls. `create_host` now
orders the VPS and then calls it, so there is exactly one "set up the host after
the VPS exists" code path.

Added `teardown_container_on_existing_vps` to remove a host's container + per-host
btrfs subvolume + named volumes on an already-reachable VPS (no VPS-API calls),
for rebuilding a container in place.

Added `ExternallyManagedVpsClient`, a `VpsClientInterface` stub for providers that
operate on a VPS they did not order (e.g. an imbue_cloud-leased pool host); every
ordering/snapshot/ssh-key call raises so a wrong call site fails loudly.

These are consumed by `mngr_imbue_cloud`'s new slow path; existing OVH/Vultr
behavior is unchanged.

## 2026-06-02

Simplified exception handlers now that `HostError`/`HostConnectionError` are `MngrError`
subclasses: the redundant `except (HostConnectionError, MngrError)` guards in the VPS Docker
instance are now just `except MngrError`. No behavior change -- host connection errors are
still caught and handled the same way.

## 2026-06-01

# Offline agent field generators

Updated the provider's `get_host_and_agent_details` override to accept and forward the new `offline_field_generators` parameter to the base implementation, so offline plugin fields (see the mngr changelog entry) are populated when a host falls back to offline data.

## 2026-05-29

User-visible: minds workspaces running on docker-on-VPS hosts can now be
backed up off-site (restic) when a backup provider is selected at creation
time; the outer-trigger btrfs snapshot path these hosts use is what the
backup service reads from.

(No code change in this project in this PR; the integration lives in the
minds app and the forever-claude-template `host_backup` service.)

Provisioned a per-host outer-side btrfs snapshot helper for the new
forever-claude-template `host_backup` service. Each vps-docker host now
gets:

- `/usr/local/sbin/snapshot_helper.sh` + `snapshot_helper.service` (a
  systemd unit shipped as a bundled resource in
  `imbue/mngr_vps_docker/resources/`) that watches a per-host docker
  volume `mngr-snapshot-trigger-<host_id_hex>` for `request.json` files
  and produces matching `result.json` files describing the outcome of
  `btrfs subvolume snapshot` / `btrfs subvolume delete` against the
  per-host subvolume.
- That docker volume is mounted into the agent container at
  `/mngr-snapshot/` so the in-container `host_backup` script can do the
  RPC; the outer's `<btrfs-mount>/snapshots/` directory is bind-mounted
  read-only into the container at `/mngr-snapshots/` so restic can read
  the snapshot the helper produced.
- Cloud-init now installs `inotify-tools` and `jq` so the helper has
  what it needs at boot.
- `destroy_host` removes the per-host snapshot-trigger volume alongside
  the existing host-volume cleanup.

The per-host unified docker volume on Vultr / OVH VPSes is now backed by a btrfs
subvolume on a loop-mounted btrfs filesystem on the VPS, so the host's agent
data is eligible for consistent `btrfs subvolume snapshot -r` snapshots.

Concretely, `VpsDockerProvider._setup_container_on_vps` now begins by calling a
new `_prepare_btrfs_on_outer` step that, idempotently and on demand, installs
`btrfs-progs`, `fallocate`-allocates `/var/lib/mngr-btrfs.img` (sized to the
outer's free space minus a configurable reservation), `mkfs.btrfs`'s it,
loop-mounts it at `/mngr-btrfs`, persists the mount in `/etc/fstab`, and
creates a per-host subvolume at `/mngr-btrfs/<host_id_hex>`. The unified
docker volume (`mngr-host-vol-<host_id_hex>`) is then created with
`--driver=local --opt type=none --opt device=/mngr-btrfs/<host_id_hex> --opt o=bind`,
so its real on-disk storage is the btrfs subvolume; `host_store.py` reads the
bind-source path out of `Options.device` instead of the docker-managed
`Mountpoint`. `destroy_host` runs a best-effort `btrfs subvolume delete`
immediately before removing the docker volume (VPS-destroy nukes the loop file
otherwise).

Docker itself still uses default `data-root=/var/lib/docker` and
`storage-driver=overlay2` on the ext4 root; only this one volume's storage is
on btrfs. Three new fields on `VpsDockerProviderConfig` make the layout
configurable: `btrfs_mount_path` (default `/mngr-btrfs`),
`btrfs_loop_file_path` (default `/var/lib/mngr-btrfs.img`), and
`outer_disk_reserved_gb` (default 20).

**Breaking change:** existing vultr / ovh hosts created on the prior
plain-`docker-volume-create` layout cannot be discovered or managed after
upgrade. Destroy and recreate them.

Consolidated the `docker_vps` provider's two-volume layout (per-user state container
volume + per-host data volume) into a single per-host Docker volume on the VPS. The
unified volume `mngr-host-vol-<host_id_hex>` now holds `host_state.json`,
`agents/<agent_id>.json`, and `host_dir/` side by side, mounted at `/mngr-vol` inside
the agent container with `/mngr` symlinked to `/mngr-vol/host_dir`. mngr now reads
and writes metadata directly on the VPS filesystem via the volume's docker mountpoint
(discovered with `docker volume inspect`); the dedicated Alpine "state container" and
the per-user `docker-state-<user_id>` volume are no longer created or read.

This makes future single-volume backup of a host straightforward (one
`docker run --rm -v <volume>:/data ...` captures everything) and removes a layer of
indirection that existed only for historical symmetry with the local `docker` provider.

**Breaking change:** existing `docker_vps` hosts created before this release cannot
be discovered or managed after upgrade. Destroy and recreate them.

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

`rsync` added to `mngr_vps_docker.cloud_init.generate_cloud_init_user_data`'s
package list for belt-and-suspenders symmetry on cloud-init backends (paired
with `mngr_ovh`'s `install_required_outer_packages` on the non-cloud-init OVH
path).

- Refactors `VpsDockerProvider` to lift the shared parallel-SSH discovery into the base class behind a new `_list_provider_vps_hostnames()` seam method (concrete in the base, returns `[]`; overridden by concrete providers); `mngr_vultr` now only contributes the tag-listing.
- Widens `os_id` in the VPS Docker base to `int | str` so providers (like OVH) can carry friendly image names through the existing build-args parser without disrupting integer-id providers (like Vultr).
