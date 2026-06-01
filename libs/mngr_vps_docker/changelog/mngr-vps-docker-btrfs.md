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
