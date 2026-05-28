Added an opt-in btrfs host-data volume mode to the Lima provider. The
new `is_host_data_volume_exposed: bool = True` field on `LimaProviderConfig`
(and the matching field persisted on `LimaHostConfig` in the per-host
record) controls how `host_dir` is backed:

- `True` (default) keeps today's behavior: `host_dir` is a 9p bind mount
  of `~/.mngr/providers/lima/<name>/volumes/<host_id>/` from the host
  machine. The host can read `host_dir` contents directly even while
  the VM is stopped, and `get_volume_for_host()` returns a usable
  `HostVolume`.

- `False` attaches a Lima-managed btrfs `additionalDisk`
  (`mngr-<host_id_hex>-data`, 100GiB default logical size, qcow2 sparse
  storage under `~/.lima/_disks/`), bind-mounts it at
  `/mnt/host-volume` inside the VM, symlinks `host_dir` into it, and
  omits the 9p mount entirely. This makes `host_dir` snapshottable as
  a single consistent btrfs filesystem. `get_volume_for_host()` returns
  `None` in this mode; callers (events API, mngr_claude session
  preservation, mngr_tmr, mngr_file) already degrade gracefully.

The chosen value is locked on the per-host record at create time so
`stop_host` / `start_host` always replay the same layout. Records that
predate the field default to `True`, preserving today's behavior for
all existing Lima hosts. `destroy_host` and `delete_host` now also
remove the named Lima disk when a host was created in btrfs mode.

A new `host_data_disk_size` config field (default `"100GiB"`) and a
new `limactl_disk_delete` helper in `limactl.py` round out the
plumbing. Snapshot/backup API support stays out of scope for this
change (`supports_snapshots` remains `False`).
