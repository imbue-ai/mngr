# VPS docker host volume on btrfs

## Overview

- Today the per-host unified docker volume on Vultr / OVH VPSes (`mngr-host-vol-<host_id_hex>`) is a plain `docker volume create` directory on `/var/lib/docker/volumes/...`; it's an ext4 directory, so `btrfs subvolume snapshot -r` is not an option.
- Goal: stage the storage layout so the unified host volume's on-disk content lives on a btrfs filesystem, as a real btrfs subvolume — without changing docker's own `/var/lib/docker` or `storage-driver=overlay2`.
- Provision btrfs as a sparse-but-`fallocate`-reserved image file on the existing ext4 root, loop-mounted via `/etc/fstab` — no re-partitioning, no rescue boot, no cloud-init dependence (works identically on Vultr and OVH).
- Scope: only this one named volume's storage is moved to btrfs. The docker daemon keeps default `data-root=/var/lib/docker` and `storage-driver=overlay2`. No third-party btrfs volume driver/plugin.
- The named docker volume continues to exist, created with `--driver=local --opt type=none --opt device=<btrfs_mount_path>/<host_id_hex> --opt o=bind`; the actual data lives in the btrfs subvolume the `device=` option points at.
- Existing vultr / ovh hosts are not migrated — declared a breaking change like the prior "two-volume consolidation"; operators destroy and recreate.
- Out of scope: rewriting `create_snapshot` to take `btrfs subvolume snapshot -r` — that's a follow-up PR. This PR only puts the volume *on* btrfs.

## Expected Behavior

- `mngr create --provider vultr` and `mngr create --provider ovh` produce a VPS where:
  - `/var/lib/mngr-btrfs.img` exists, is `fallocate`-reserved, and is `mkfs.btrfs`-formatted.
  - `/mngr-btrfs` is a btrfs mount of that image file, loop-mounted via an `/etc/fstab` line (`/var/lib/mngr-btrfs.img  /mngr-btrfs  btrfs  loop,defaults  0 0`), so the mount survives VPS reboots.
  - `<btrfs_mount_path>/<host_id_hex>` is a real btrfs subvolume (created with `btrfs subvolume create`).
  - The docker named volume `mngr-host-vol-<host_id_hex>` exists with `Options.device = <btrfs_mount_path>/<host_id_hex>` and `o=bind`. `docker volume inspect`'s `.Mountpoint` is the unused `/var/lib/docker/volumes/<name>/_data` placeholder.
  - The agent container mounts `mngr-host-vol-<host_id_hex>` at `/mngr-vol` exactly as today; the container sees the same `host_state.json` / `agents/` / `host_dir/` layout it sees today.
- `host_store.py` reads / writes `host_state.json`, `agents/<id>.json`, and `host_dir/...` at the btrfs subvolume path it learns from `docker volume inspect --format '{{.Options.device}}'` (the docker volume remains the single source of truth for the on-disk path).
- The per-VPS btrfs setup (`_prepare_btrfs_on_outer`) runs once at host-create time and is idempotent step-by-step: skips loop-file allocation, skips mkfs + mount, skips subvolume create, skips the `/etc/fstab` line append, and skips the `btrfs-progs` apt install when each is already in place. Never `mkfs.btrfs -f`. Safe to re-run after a partially-failed earlier `mngr create`.
- When free space on `/` is less than `outer_disk_reserved_gb` at provisioning time, `mngr create` fails with `VpsProvisioningError` naming the actual free-vs-reserved numbers; the existing `create_host` cleanup destroys the VPS so nothing leaks.
- `mngr stop` / `mngr start` only touch the container, as today; the loop mount stays up (and is restored from `/etc/fstab` if the VPS itself reboots). On `start_host`, no explicit btrfs-mount precheck — `docker start` surfaces a bind-source-missing failure naturally as `HostConnectionError` on the next operation if the mount silently failed at boot.
- `mngr destroy` runs a best-effort `btrfs subvolume delete <btrfs_mount_path>/<host_id_hex>` immediately before the existing `_remove_volume(mngr-host-vol-<host_id_hex>)` call, with the same `try/except (HostConnectionError, MngrError)` + `logger.warning` shape that `_remove_volume` already uses. The VPS-destroy that follows nukes the loop file regardless, so this is primarily belt-and-suspenders for the rare retried-destroy-on-still-existing-VPS case.
- Existing pre-change vultr / ovh hosts on the old named-volume layout are not discoverable / not manageable after upgrade — same breaking-change shape as the prior "two-volume consolidation"; the changelog calls this out explicitly and instructs destroy + recreate.
- No CLI flag and no provider-specific configuration knob: the new fields are all on the shared `VpsDockerProviderConfig` and are inherited by both `VultrProviderConfig` and `OvhProviderConfig`.

## Changes

- Add three new fields to `VpsDockerProviderConfig`:
  - `btrfs_mount_path: Path = Path("/mngr-btrfs")` — where the loop-mounted btrfs FS lives on the outer.
  - `btrfs_loop_file_path: Path = Path("/var/lib/mngr-btrfs.img")` — the loop-backed image file on the outer's ext4 root.
  - `outer_disk_reserved_gb: int = 20` — how much of the outer's free space on `/` is held back from btrfs at provisioning time (loop file size = free space at provisioning - this).
- Add a new base-class method `VpsDockerProvider._prepare_btrfs_on_outer(outer, host_id)` that, on the outer, does:
  - `apt-get install -y btrfs-progs` (idempotent; skip if `command -v mkfs.btrfs` already resolves).
  - Check free space on `/`; raise `VpsProvisioningError` with concrete numbers when `free_gb - outer_disk_reserved_gb` is non-positive.
  - If `btrfs_loop_file_path` does not exist: `fallocate -l <computed-size>G <path>` and `mkfs.btrfs <path>`.
  - Ensure `btrfs_mount_path` exists; if it is not already a mounted btrfs filesystem, `mount -o loop <btrfs_loop_file_path> <btrfs_mount_path>`.
  - If the fstab line is not yet present, append `/var/lib/mngr-btrfs.img  /mngr-btrfs  btrfs  loop,defaults  0 0`.
  - If `<btrfs_mount_path>/<host_id_hex>` does not exist, `btrfs subvolume create <btrfs_mount_path>/<host_id_hex>`.
  - Each step wrapped in a `log_span` of its own; failures raise `VpsProvisioningError` (cleanup of the freshly-created VPS is handled by the existing `create_host` except branch).
- Call `_prepare_btrfs_on_outer(outer, host_id)` at the top of `VpsDockerProvider._setup_container_on_vps`, before the docker volume creation. Vultr and OVH both go through it; neither provider's bootstrap (`cloud_init.py`'s `packages:` / `runcmd:`, OVH's `_REQUIRED_OUTER_PACKAGES`) needs to know about btrfs.
- Replace the current `create_volume_with_layout(outer, volume_name, host_dir_subpath)` call site:
  - The subvolume created by `_prepare_btrfs_on_outer` already serves as the data root.
  - `docker volume create --driver=local --opt type=none --opt device=<btrfs_mount_path>/<host_id_hex> --opt o=bind <volume_name>` produces the named volume.
  - Seed the `host_dir/` and `agents/` subdirectories under the subvolume path directly (the existing seed-layout shell command already mkdir's them; just point it at the subvolume path).
- Update `host_store.py`:
  - `resolve_volume_mountpoint` becomes `resolve_volume_device(outer, volume_name)` (or similar) and reads `docker volume inspect --format '{{.Options.device}}'` instead of `{{.Mountpoint}}`. Empty / missing `device` is a hard error.
  - `open_host_store(outer, volume_name)` calls the renamed helper and binds the store to the bind-device path. The store class itself does not need to change — it already takes an absolute `mountpoint: Path` and writes to subpaths.
  - `create_volume_with_layout` is removed; the responsibility splits between `_prepare_btrfs_on_outer` (subvolume + base path) and the new `docker volume create --opt` call in `_setup_container_on_vps`.
- Update `VpsDockerProvider.destroy_host`:
  - Before the existing `_remove_volume(outer, vps_config.volume_name)` call, add a best-effort `btrfs subvolume delete <btrfs_mount_path>/<host_id_hex>` shell call, wrapped in the same `try/except (HostConnectionError, MngrError): logger.warning(...)` pattern as the existing `_remove_volume` call.
  - Do not touch the loop file or `/etc/fstab` line; VPS-destroy takes them with it.
- Update `libs/mngr_vps_docker/README.md`:
  - Document the new btrfs layout (loop file at `/var/lib/mngr-btrfs.img`, mount at `/mngr-btrfs`, one subvolume per host).
  - Update the ASCII architecture diagram so the unified volume is shown as backed by a btrfs subvolume, not a plain docker volume directory.
  - Document the new `btrfs_mount_path` / `btrfs_loop_file_path` / `outer_disk_reserved_gb` config fields in the configuration table.
  - Add a "Breaking change" note next to the existing one for the unified-volume consolidation.
- Add a per-PR changelog entry in each of the three affected projects (branch `mngr/vps-docker-btrfs`, slashes replaced with dashes):
  - `libs/mngr_vps_docker/changelog/mngr-vps-docker-btrfs.md` — base provider change.
  - `libs/mngr_vultr/changelog/mngr-vps-docker-btrfs.md` — user-visible breaking change for vultr hosts (destroy + recreate).
  - `libs/mngr_ovh/changelog/mngr-vps-docker-btrfs.md` — user-visible breaking change for ovh hosts (destroy + recreate).
- Out of scope: any change to `create_snapshot` (still `docker commit` for now); any change to the local `mngr_file` / `mngr_docker` providers; any migration path for pre-change vultr / ovh hosts; per-volume btrfs snapshot management; making the subvolume name configurable.
