# mngr VPS Docker Provider

Base classes and shared infrastructure for running mngr agents in Docker containers on VPS instances.

This package is a library -- it provides abstract base classes that concrete VPS provider implementations (like `mngr_vultr`) build on. It does not register any provider backends itself.

## Architecture

Each VPS runs exactly one Docker container (1:1 mapping). Docker is used purely as a consistent provisioning mechanism. The VPS stays running at all times; stop/start operates on the container. Destroying the host destroys both the container and the VPS.

```
User Machine                              VPS
+------------------+                      +-------------------------------+
|                  |   SSH (port 22)      |  VPS OS (Debian/Ubuntu)       |
|  mngr CLI        | ------------------> |  (Docker commands over SSH)   |
|                  |                      |  Docker Engine                |
|  ~/.mngr/        |   SSH (port 2222)   |  +-------------------------+ |
|    profile/      | ------------------> |  | Container (sshd)        | |
|      providers/  |   direct to         |  |  /mngr/ (host_dir)     | |
|        <backend>/|   VPS:2222          |  +-------------------------+ |
|          keys/   |                      |  Docker named volume          |
+------------------+                      |  State container + volume     |
                                          +-------------------------------+
```

### Key design decisions

- **Docker commands over SSH**: All Docker operations are executed via `ssh user@vps docker ...`, not via the Docker SDK's remote host feature.
- **Direct SSH to container**: The container's sshd port (default 2222) is exposed on the VPS's public IP. mngr connects directly to `<vps_ip>:2222` with key-based authentication.
- **SSH host keys via cloud-init**: Host keys are generated locally and injected into the VPS via cloud-init `user_data`, eliminating TOFU (trust-on-first-use).
- **State on the VPS**: All host records and agent data are stored on a Docker state volume on the VPS itself, following the same pattern as the existing Docker provider (state container + named volume).
- **Separate SSH keypairs**: The VPS and container each have their own SSH keypair for defense in depth.

## Modules

- `vps_client.py` -- Abstract `VpsClientInterface` that concrete providers implement (create/destroy instances, snapshots, SSH key management)
- `instance.py` -- `VpsDockerProvider` implementation with full lifecycle (create, stop, start, destroy, snapshots, discovery)
- `docker_over_ssh.py` -- `DockerOverSsh` helper for executing Docker commands on a remote VPS via SSH
- `host_store.py` -- `VpsDockerHostStore` for reading/writing host records on the VPS state volume
- `cloud_init.py` -- Cloud-init user_data generation for VPS provisioning
- `config.py` -- `VpsDockerProviderConfig` base configuration
- `errors.py` -- Error hierarchy (`VpsDockerError`, `VpsProvisioningError`, etc.)
- `primitives.py` -- VPS-specific types (`VpsInstanceId`, `VpsInstanceStatus`, etc.)

## Configuration

The base config (`VpsDockerProviderConfig`) provides these settings:

| Field | Default | Description |
|-------|---------|-------------|
| `host_dir` | `/mngr` | Base directory for mngr data inside containers |
| `default_image` | `debian:bookworm-slim` | Default Docker image |
| `default_idle_timeout` | 800 | Idle timeout in seconds |
| `default_idle_mode` | `IO` | Idle detection mode |
| `ssh_connect_timeout` | 60.0 | SSH connection timeout in seconds |
| `vps_boot_timeout` | 300.0 | VPS provisioning timeout in seconds |
| `docker_install_timeout` | 300.0 | Docker installation timeout in seconds |
| `container_ssh_port` | 2222 | Container sshd port exposed on VPS |
| `default_region` | `ewr` | Default VPS region |
| `default_plan` | `vc2-1c-1gb` | Default VPS plan |
| `default_os_id` | 2136 | Default OS image (Debian 12 x64) |
| `default_start_args` | `()` | Default `docker run` arguments |

## Build and start args

Build args (`-b`) serve two purposes: VPS provisioning and Docker image building.

**VPS-specific args** use the `--vps-` prefix and are consumed by the provider:
```
--vps-region=ewr          # VPS region
--vps-plan=vc2-2c-4gb     # VPS plan (CPU/RAM)
--vps-os=2136             # VPS OS ID
```

**All other build args** are passed through to `docker build` on the VPS. This follows the same pattern as the Docker provider:
```
--file=Dockerfile     # Use a specific Dockerfile
.                     # Build context (local directory, uploaded to VPS)
```

VPS provider implementations must not use any flags that conflict with Docker build flags. All VPS-specific flags must use the `--vps-` prefix.

**Example**: Create a host with a custom Dockerfile on a specific VPS plan:
```bash
mngr create my-agent --provider vultr -b --vps-plan=vc2-2c-4gb -b --file=Dockerfile -b .
```

**Start args** (`-s`) are passed to `docker run`:
```
--cpus=2              # CPU limit for container
--memory=4g           # Memory limit
```

## Host lifecycle

| Operation | What happens |
|-----------|-------------|
| `create` | Provision VPS, install Docker via cloud-init, run container, setup SSH, write state |
| `stop` | `docker stop` the container. VPS keeps running. |
| `start` | `docker start` the container. Wait for SSH. |
| `destroy` | Remove container and volume, destroy VPS, clean up SSH keys |
| idle timeout | `docker stop` the container. VPS keeps running. |

## Implementing a new VPS provider

To add support for a new VPS provider (e.g., DigitalOcean, Hetzner):

1. Create a new package (e.g., `mngr_digitalocean`)
2. Implement `VpsClientInterface` with the provider's API
3. Subclass `VpsDockerProvider` and override `_discover_host_records()` and `_find_host_record()` to use the provider's instance listing API
4. Create a `ProviderBackendInterface` implementation and register via pluggy entry points
