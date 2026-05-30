Vultr hosts created by `mngr create --provider vultr` now back their per-host
unified docker volume with a btrfs subvolume on a loop-mounted btrfs filesystem
on the VPS (`/mngr-btrfs/<host_id_hex>` on `/var/lib/mngr-btrfs.img`). This
makes future consistent snapshotting of the agent data via
`btrfs subvolume snapshot -r` possible. See `mngr_vps_docker`'s changelog for
the full mechanism.

**Breaking change:** existing vultr hosts created before this release cannot
be discovered or managed after upgrade. Destroy and recreate them.
