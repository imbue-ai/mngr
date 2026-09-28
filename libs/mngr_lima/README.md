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

## host_dir layout

`host_dir` is the in-VM directory mngr stores agent data under (default `/mngr`). There are two layouts, chosen by `is_host_data_volume_exposed` and locked in per-host at create time (changing the config later does not migrate existing hosts):

- **Exposed (default, `true`).** The host machine can read `host_dir` directly, even while the VM is stopped. Offline reads (`mngr event`, `mngr transcript`) work against a stopped host. Right for most users.
- **Btrfs disk (`false`).** `host_dir` lives on a single btrfs disk that can be snapshotted as one unit, but the host machine has no direct read path, so offline reads stop working until the host is started. Intended for users who back up `host_dir` via btrfs snapshots.

```toml
# in ~/.mngr/config.toml (or via setting__extend in a create template)
[providers.lima]
is_host_data_volume_exposed = false
# Optional: override the default 100GiB logical disk size.
host_data_disk_size = "200GiB"
```

## Sizing and resize

A host's CPUs and memory are the `--cpus` / `--memory` start args its VM was created with (`-s --cpus=2 -s --memory=4` on `mngr create`, in GiB); `--disk` there is the VM's boot disk. In the btrfs disk layout the host's data lives on the separate data disk, sized by `host_data_disk_size` at create. Both are recorded per host, and `mngr list` reports them as `host.resource` (`cpu.count`, `memory_gb`, and `disk_gb`, which is the data disk when the host has one, else the boot disk) for running and stopped hosts alike.

To change a host's size afterwards:

```bash
mngr lima resize my-host --cpus 4 --memory 8 --disk 200
```

The CPU count and memory are written into the instance's lima config, and `--disk` grows the data disk with `limactl disk resize` (a disk never shrinks, and a host on the exposed layout has no data disk to resize). A stopped VM is reconfigured at once. A running VM cannot be edited, so the new size is only recorded and `mngr start` applies it, as it does on every start: the recorded size is the source of truth for the VM. The data filesystem grows to fill its disk on every boot, so a grown disk is usable as soon as the VM is back up.

## What lives on which disk

The boot disk holds the guest OS and whatever provisioning installs into it, and cannot grow after creation; the data disk backs `host_dir` (or the whole home tree with `volume_home_path`) and can. Every VM's journal is capped at 512 MiB so it cannot fill the boot disk. Anything else that grows over a host's life (package caches, build caches) belongs on the data disk; a workspace template can put it there itself, since the agent runs as root there.

## Running the agent as root (`is_run_as_root`)

By default the agent runs as the Lima default user (with passwordless `sudo`). With `is_run_as_root=true` the agent instead runs as root inside the VM, matching the `docker` / `vps_docker` providers: it can `apt install` and write anywhere with no `sudo`, and the VM is the isolation boundary. This lets a workspace use the same idempotent setup scripts everywhere -- run by a `Dockerfile` on the container providers, and directly after sync on Lima.

This mode requires the btrfs disk layout, so it must be paired with `is_host_data_volume_exposed=false` (the combination is otherwise rejected at config construction).

```toml
[providers.lima]
is_run_as_root = true
is_host_data_volume_exposed = false  # required by is_run_as_root
```
