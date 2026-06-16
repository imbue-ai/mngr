# mngr VPS Docker Provider

Base classes and shared infrastructure for running mngr agents in Docker containers on VPS instances.

This package is a library -- it provides abstract base classes that concrete VPS provider implementations (like `mngr_vultr`) build on. It does not register any provider backends itself.

## Architecture

Each VPS runs exactly one Docker container (1:1 mapping). Docker is used purely as a consistent provisioning mechanism. The VPS stays running at all times; stop/start operates on the container. Destroying the host destroys both the container and the VPS.

```
User Machine                              VPS
+------------------+                      +-----------------------------------------+
|                  |   SSH (port 22)      |  VPS OS (Debian/Ubuntu)                 |
|  mngr CLI        | ------------------>  |  (Docker commands over SSH)             |
|                  |                      |  Docker Engine (overlay2 on ext4 root)  |
|  ~/.mngr/        |   SSH (port 2222)    |  +-----------------------------------+  |
|    profile/      | ------------------>  |  | Container (sshd)                  |  |
|      providers/  |   direct to          |  |   /mngr -> /mngr-vol/host_dir     |  |
|        <backend>/|   VPS:2222           |  +-----------------------------------+  |
|          keys/   |                      |  Docker named volume                    |
+------------------+                      |  (mngr-host-vol-<host_id_hex>) is a     |
                                          |  bind-options local volume whose        |
                                          |  device= points at:                     |
                                          |    /mngr-btrfs/<host_id_hex>            |
                                          |  (per-host btrfs subvolume on a         |
                                          |   loop-mounted /var/lib/mngr-btrfs.img) |
                                          |    host_state.json                      |
                                          |    agents/<agent_id>.json               |
                                          |    host_dir/...                         |
                                          +-----------------------------------------+
```

### Key design decisions

- **Docker commands over SSH**: All Docker operations are executed via `ssh user@vps docker ...`, not via the Docker SDK's remote host feature.
- **Direct SSH to container**: The container's sshd port (default 2222) is exposed on the VPS's public IP. mngr connects directly to `<vps_ip>:2222` with key-based authentication.
- **SSH host keys via cloud-init**: Host keys are generated locally and injected into the VPS via cloud-init `user_data`, eliminating TOFU (trust-on-first-use).
- **Per-host docker volume on a btrfs subvolume**: Each VPS has exactly one mngr-managed Docker named volume (`mngr-host-vol-<host_id_hex>`), created with `--driver=local --opt type=none --opt device=/mngr-btrfs/<host_id_hex> --opt o=bind`. The `device=` path is a real btrfs subvolume on a loop-mounted btrfs filesystem (image file `/var/lib/mngr-btrfs.img`, mounted at `/mngr-btrfs` via `/etc/fstab`), which makes the per-host data eligible for `btrfs subvolume snapshot -r` for consistent snapshots. mngr reads and writes metadata (`host_state.json`, `agents/<agent_id>.json`, `host_dir/`) directly on the subvolume by extracting `Options.device` from `docker volume inspect`. Docker itself keeps default `data-root=/var/lib/docker` and `storage-driver=overlay2` (on the ext4 root); only this single volume's storage lives on btrfs.
- **Separate SSH keypairs**: The VPS and container each have their own SSH keypair for defense in depth.

## Modules

- `vps_client.py` -- Abstract `VpsClientInterface` that concrete providers implement (create/destroy instances, SSH key management)
- `instance.py` -- `VpsProvider` implementation with full lifecycle (create, stop, start, destroy, snapshots, discovery)
- `host_store.py` -- `VpsHostStore` for reading/writing host records on the unified per-host volume; constructed via `open_host_store(outer, volume_name)`
- `cloud_init.py` -- Cloud-init user_data generation for VPS provisioning
- `config.py` -- `VpsProviderConfig` base configuration
- `errors.py` -- Error hierarchy (`VpsError`, `VpsProvisioningError`, etc.)
- `primitives.py` -- VPS-specific types (`VpsInstanceId`, `VpsInstanceStatus`, etc.)

## Configuration

The base config (`VpsProviderConfig`) provides these settings:

| Field | Default | Description |
|-------|---------|-------------|
| `host_dir` | `/mngr` | Base directory for mngr data inside containers |
| `default_image` | `debian:bookworm-slim` | Default Docker image |
| `default_idle_timeout` | 800 | Idle timeout in seconds |
| `default_idle_mode` | `IO` | Idle detection mode |
| `ssh_connect_timeout` | 60.0 | SSH connection timeout in seconds |
| `instance_boot_timeout` | 300.0 | Timeout for the cloud instance to become reachable, in seconds |
| `docker_install_timeout` | 300.0 | Docker installation timeout in seconds |
| `container_ssh_port` | 2222 | Container sshd port exposed on VPS |
| `default_region` | `ewr` | Default cloud region (provider subclasses override the default) |
| `default_start_args` | `()` | Default `docker run` arguments |
| `btrfs_mount_path` | `/mngr-btrfs` | Outer-host path where the loop-mounted btrfs filesystem holding the per-host unified volume is mounted |
| `btrfs_loop_file_path` | `/var/lib/mngr-btrfs.img` | Outer-host path of the loop-backed btrfs image file (allocated with `fallocate`, persisted across reboots via `/etc/fstab`) |
| `outer_disk_reserved_gb` | `20` | GB of free space on the outer's root filesystem to reserve at provisioning time; loop file size is `free_gb - outer_disk_reserved_gb` |

## Build and start args

Build args (`-b`) serve two purposes: VPS provisioning and Docker image building.

**Provider-specific args** use a per-provider prefix (`--aws-`, `--vultr-`, `--ovh-`) and are consumed by the provider. Example shape for the common knobs:

```
--<provider>-region=REGION       # Cloud region (aws / vultr / ovh)
--<provider>-instance-type=TYPE  # AWS only — EC2 instance type
--<provider>-plan=PLAN           # Vultr / OVH plan
--ovh-datacenter=DC              # OVH-specific alias for --ovh-region=
--git-depth=N                    # Shared — git clone depth (about the *local* mngr build context)
```

The old shared `--vps-*` prefix is no longer accepted. Migration: rename `--vps-region=` to `--aws-region=` / `--vultr-region=` / `--ovh-region=` for the relevant provider; rename `--vps-plan=` to `--aws-instance-type=` (AWS) / `--vultr-plan=` / `--ovh-plan=`.

**All other build args** are passed through to `docker build` on the VPS:
```
--file=Dockerfile     # Use a specific Dockerfile
.                     # Build context (local directory, uploaded to VPS)
```

Provider implementations must not use flags that conflict with Docker build flags. All provider-specific flags must use their provider prefix.

**Example**: Create a host with a custom Dockerfile on a specific Vultr plan:
```bash
mngr create my-agent --provider vultr -b --vultr-plan=vc2-2c-4gb -b --file=Dockerfile -b .
```

**Start args** (`-s`) are passed to `docker run`:
```
--cpus=2              # CPU limit for container
--memory=4g           # Memory limit
```

## Host lifecycle

| Operation | What happens |
|-----------|-------------|
| `create` | Provision VPS, install Docker via cloud-init, prepare the btrfs loop filesystem on the outer (install `btrfs-progs`, `fallocate` + `mkfs.btrfs` the loop file, loop-mount it, persist via `/etc/fstab`, `btrfs subvolume create` the per-host subvolume), create the bind-options unified `mngr-host-vol-<hex>` volume pointing at that subvolume (seeded with empty `host_dir/` and `agents/`), run container, set up SSH, write `host_state.json` |
| `stop` | `docker stop` the container. VPS keeps running. |
| `start` | `docker start` the container. Wait for SSH. |
| `destroy` | Remove container, best-effort `btrfs subvolume delete` of the per-host subvolume (drops `host_state.json`, `agents/`, and `host_dir/` together), remove the docker named volume entry, destroy VPS (which also takes the loop file with it), clean up SSH keys |
| idle timeout | `docker stop` the container. VPS keeps running. |

## Implementing a new VPS provider

To add support for a new VPS provider (e.g., DigitalOcean, Hetzner):

1. Create a new package (e.g., `mngr_digitalocean`)
2. Implement `VpsClientInterface` with the provider's API
3. Subclass `VpsProvider` and override the two discovery extension points:
   - `_list_provider_vps_hostnames()` — return SSH-reachable hostnames (public IPv4 or provider DNS name like OVH's `serviceName`) for VPSes tagged with `mngr-provider=<self.name>`
   - `_credentials_configured()` — return whether the provider's API credentials are resolvable
   The shared discovery flow (SSH-into-each-VPS, read state container, fall back to cache on failure)
   lives on `VpsProvider` itself; subclasses only need to wire up these two hooks.
4. Create a `ProviderBackendInterface` implementation and register via pluggy entry points

The btrfs loop-file setup is provided by the base class (`_prepare_btrfs_on_outer`, called at the top of `_setup_container_on_vps`); new providers do not need to install `btrfs-progs` or wire up the loop mount themselves as long as the outer host is a Debian-family Linux with `apt-get` available.

## Compatibility

This release moves the per-host unified docker volume onto a loop-mounted btrfs filesystem (the volume itself becomes a bind-options local volume backed by `<btrfs_mount_path>/<host_id_hex>`). Existing vultr / ovh hosts created on the prior plain-`docker-volume-create` layout cannot be discovered or managed after upgrade. Destroy and recreate them. This is the same breaking-change shape as the earlier "two-volume consolidation" change.
