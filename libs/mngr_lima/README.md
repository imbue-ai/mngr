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

## Running the agent in a Docker container in the VM (`is_host_in_docker`)

By default the agent runs directly inside the Lima VM. With
`is_host_in_docker=True` the provider instead provisions the VM with only
Docker + btrfs + an SSH "outer", and runs the agent inside a **Docker
container** in the VM, built from the project's `Dockerfile` exactly like the
`docker` and `vps_docker` providers. The point is consistency: every provider
installs dependencies the same way (one Dockerfile, no `sudo`, always in sync),
instead of duplicating the Dockerfile's install steps in a Lima-specific
provisioning script.

How it works:

- mngr treats the **container as the host**: `mngr connect` / `mngr exec` /
  ssh land inside the container, and Lima forwards the container's sshd out to
  a unique `127.0.0.1:<port>` on your machine. The VM itself is an "outer"
  that mngr otherwise does not touch (mirroring how the `vps_docker` provider
  treats its VPS).
- This mode **requires** the btrfs additional-disk layout, so it forces
  `is_host_data_volume_exposed=false`. A per-host btrfs *subvolume* on that
  disk backs the container's `host_dir`, bind-mounted in as a Docker volume.
- Consistent backups work from inside the container: the `mngr_vps_docker`
  snapshot helper is installed in the VM, and the in-container agent triggers
  `btrfs subvolume snapshot` via the same `/mngr-snapshot` request /
  `/mngr-snapshots` read contract the other Docker providers use.
- `mngr stop` powers off the whole VM (freeing local RAM); `start` boots the
  VM and relaunches the container; `destroy` removes the VM and the disk.
- The image is built inside the VM via `mngr create ... -b "--file=Dockerfile" -b "."`
  (same build args as the `docker` provider). Without build args, the
  `default_image` is pulled instead.
- Like btrfs mode, offline reads (`mngr event` / `mngr transcript`) against a
  stopped host do not work until it is started.

```toml
[providers.lima]
is_host_in_docker = true
is_host_data_volume_exposed = false  # required by is_host_in_docker
```
