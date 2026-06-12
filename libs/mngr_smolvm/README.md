# mngr smolvm Provider

smolvm microVM provider backend plugin for mngr. Runs agents in smolvm machines (libkrun microVMs: KVM on Linux, Hypervisor.framework on macOS) with SSH access.

## Prerequisites

- `smolvm` on PATH (or set `providers.smolvm.smolvm_command`), version >= 1.0.3
- Linux: a usable `/dev/kvm` (smolvm has no emulation fallback)
- macOS: an HVF-entitled smolvm binary
- The btrfs storage layout (`is_host_data_volume_exposed=false`) additionally requires a smolvm build with persistent data-disk support (a btrfs-enabled guest kernel and the `--data-disk` machine flag)

## How it works

- Each mngr host is a smolvm machine named `mngr-<host>`. With no `--image`, the host is a bare Alpine VM; with `--image`, the OCI image runs as the workload container. `--image-archive PATH` (a `docker save` tarball) is converted to a cached `.smolmachine` pack and used directly, so locally built images work without a registry.
- mngr provisions sshd inside the guest over the smolvm exec channel (installing openssh, tmux, git, rsync, jq via apk/apt on first boot), injects a pre-generated host key and its client key, and connects over a forwarded localhost port.
- Storage layouts: the default exposes host_dir to the host machine via virtiofs (offline file reads work while the VM is stopped); the btrfs layout (`is_host_data_volume_exposed=false`) mounts a smolvm-managed btrfs data disk at host_dir for consistent, unprivileged snapshots inside the guest.
- Idle hosts stop themselves: the shutdown script touches `/run/smolvm/poweroff` and the smolvm guest agent syncs and powers the VM off.

## Configuration

```toml
[providers.smolvm]
is_enabled = true
# smolvm_command = "/path/to/custom/smolvm"
# is_host_data_volume_exposed = false   # btrfs data-disk layout
# host_data_disk_size_gb = 100
```

## Usage

```bash
mngr create my-agent@.smolvm
mngr create my-agent@.smolvm --image alpine:3.19
mngr create my-agent@.smolvm -b "--image-archive /tmp/myimg.tar"
```
