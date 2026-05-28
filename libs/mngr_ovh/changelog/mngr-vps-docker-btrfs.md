OVH hosts created by `mngr create --provider ovh` now back their per-host
unified docker volume with a btrfs subvolume on a loop-mounted btrfs filesystem
on the VPS (`/mngr-btrfs/<host_id_hex>` on `/var/lib/mngr-btrfs.img`). This
makes future consistent snapshotting of the agent data via
`btrfs subvolume snapshot -r` possible. The setup happens in the shared
`VpsDockerProvider._setup_container_on_vps` path, so OVH's bootstrap (rebuild +
TOFU + root SSH + `rsync` install) is unchanged; the `apt-get install btrfs-progs`
runs on the freshly-bootstrapped root SSH session. See `mngr_vps_docker`'s
changelog for the full mechanism.

**Breaking change:** existing ovh hosts created before this release cannot
be discovered or managed after upgrade. Destroy and recreate them.
