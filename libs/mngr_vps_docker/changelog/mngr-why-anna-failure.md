Reworked the outer-side btrfs snapshot helper (`snapshot_helper.sh`) so vps-docker backups capture data on every cycle instead of only the first.

Previously the helper snapshotted into a single fixed path (`snapshots/current`), deleting and recreating it each cycle. Under gVisor (runsc) the container reads that path through the gofer, which caches a handle to the first subvolume it opened -- so after the first delete+recreate every snapshot read came back empty and restic backed up nothing.

The helper now creates each snapshot at a unique, caller-named path (`snapshots/<name>`), fails rather than overwriting on a name collision, and deletes old snapshots by name on request. Cleanup targets are validated to be a single path component (no `/` or `..`) so a malformed request can never escape the snapshots directory or touch the live subvolume. The inner `host_backup` service drives the new naming and garbage-collects old snapshots down to a retained count.
