
The in-guest data-filesystem grow oneshot (shipped in gen-2 cloud-init) only resizes btrfs mounts now: its /mnt/lima-* glob also matches a converted guest's /mnt/lima-cidata iso9660 mount, which made the oneshot fail every boot on converted machines.

The grow oneshot also grows a partitioned data disk's partition (via growpart) before the btrfs resize: a converted gen-1 machine's filesystem sits on a partition, which a qcow2 grow leaves untouched, so disk grows silently failed to land in-guest on converted machines.
