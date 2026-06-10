# mngr Lima Provider

Lima VM provider backend plugin for mngr. Runs agents in Lima VMs (QEMU/VZ) with SSH access.

## Prerequisites

- [Lima](https://lima-vm.io/docs/installation/) (`limactl` on PATH)

## Usage

```bash
# Install the plugin
uv tool install imbue-mngr-lima

# Create a VM host
mngr create @.lima

# Create with a custom Lima YAML config
mngr create @.lima -b "--file path/to/config.yaml"

# Pass flags to limactl start
mngr create @.lima -- --cpus=8 --memory=16GiB
```

## host_dir layout: bind mount vs btrfs additional disk

The provider has two layouts for backing `host_dir` (the in-VM directory
mngr stores agent data under, default `/mngr`):

- **Bind mount (default, `is_host_data_volume_exposed=True`).** `host_dir`
  is a 9p bind mount of `~/.mngr/providers/lima/<name>/volumes/<host_id>/`
  on the host machine. The host process can read `host_dir` contents
  directly even while the VM is stopped, and `get_volume_for_host()`
  returns a `HostVolume` usable by `mngr event`, `mngr transcript`, and
  destroy-time session-preservation hooks (`mngr_claude`'s
  `on_before_host_destroy`). This is the right choice for most users.

- **Btrfs additional disk (`is_host_data_volume_exposed=False`).** mngr
  attaches a Lima-managed btrfs-formatted additional disk
  (`mngr-<host_id_hex>-data`, qcow2 sparse under `~/.lima/_disks/`, default
  logical size `100GiB`) and symlinks `host_dir` to Lima's auto-mount path
  for that disk (`/mnt/lima-<disk_name>`). No 9p bind mount is created. The
  trade-off: `host_dir` is now a single consistent btrfs filesystem that
  can be snapshotted as one unit, but the host machine has no direct read
  path so `get_volume_for_host()` returns `None`. Offline reads
  (`mngr event` / `mngr transcript` against a stopped Lima host) stop
  working until the host is started. This mode is intended for users who
  back up host_dir via btrfs snapshots; it is opt-in to avoid changing
  default behavior.

The chosen layout is locked in on the per-host record at `create_host`
time. Subsequent `start_host` / `stop_host` calls always replay the
same layout, so flipping the provider config later does not migrate
existing hosts. Hosts created before this option existed (records
without the field) default to bind-mount mode and keep today's
behavior forever.

To enable btrfs mode for new hosts:

```toml
# in ~/.mngr/config.toml (or via setting__extend in a create template)
[providers.lima]
is_host_data_volume_exposed = false
# Optional: override the default 100GiB logical disk size.
host_data_disk_size = "200GiB"
```

## Running the agent as root in the VM (`is_run_as_root`)

By default the agent runs as the Lima default user (passwordless `sudo`
available). With `is_run_as_root=true` mngr instead runs the agent **as root**
inside the VM (uid 0), matching the `docker` / `vps_docker` providers where the
agent is root inside its container: the agent can `apt install` and write
anywhere with no `sudo`. The VM itself is the isolation boundary.

How it works:

- The provisioning script enables key-based root login (`PermitRootLogin
  prohibit-password`) and authorizes a mngr-managed root client key; mngr then
  connects to the VM as root over Lima's normal SSH port. The agent still runs
  directly in the VM -- there is no nested container.
- This mode **requires** the btrfs additional-disk layout, so it must be paired
  with `is_host_data_volume_exposed=false`. Root cannot traverse the
  9p/reverse-sshfs bind mount the exposed layout uses, so the combination is
  rejected at config construction.
- Consistent backups keep working: with `host_dir` on btrfs and the agent
  running as root, a backup service in the workspace (e.g.
  forever-claude-template's `host-backup`) takes `btrfs subvolume snapshot`
  directly -- no snapshot helper or trigger volume is needed.

```toml
[providers.lima]
is_run_as_root = true
is_host_data_volume_exposed = false  # required by is_run_as_root
```

This is how a workspace gets the **same** dependency setup as a Dockerfile-built
host: the project ships idempotent setup scripts that its `Dockerfile` runs (for
the `docker` / `vps_docker` / `ovh` providers) and that the Lima host runs
directly after the project is synced in (via the create template's agent
provisioning command). One definition, run the same way everywhere, with the
agent as root so it never needs `sudo`.
