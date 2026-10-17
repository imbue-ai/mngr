Reliability fixes for Lima hosts, from the mngr-internal#121 investigation:

- Creating a Lima host with a name that is already in use now fails fast with `HostNameConflictError` (naming the existing host and its state) instead of silently building a second VM under the same name. The name is reserved with a BUILDING host record before `limactl start`, so concurrent creates conflict too; FAILED and DESTROYED records do not conflict, so a name can be reused after a failed create. A stale reservation from a hard-killed create can be cleared with `mngr destroy @<host-id>` (the conflict error says so).

- `delete_host` now deletes the Lima VM definition itself (tolerating it already being gone) before removing the disk and records, so `mngr gc`'s offline-host path no longer strands VMs in `limactl list` with nothing left to manage them.

- The in-guest btrfs format now applies the `lima-<disk_name>` filesystem label Lima's own `05-lima-disks.sh` probes for, and heals the label on disks formatted before this fix. Previously the missing label made Lima re-enter its first-time disk setup on every boot -- re-running `sfdisk` against the data disk and failing `cloud-final`, leaving bare-image VMs systemd-`degraded` on every boot. The failure is now confined to the first boot on a stock image (where nothing can preinstall `btrfs-progs` ahead of Lima's boot scripts).

- A transient network failure while downloading the base image (TLS handshake timeout, connection reset, 5xx, ...) now retries `limactl start` once, cleaning up the half-created instance between attempts. Permanent failures (404/403) are never retried.
