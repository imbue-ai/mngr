# mngr VPS Docker Provider

Shared base for the mngr cloud VPS providers (`mngr_vultr`, `mngr_aws`, `mngr_ovh`). It is a library: it provides the common configuration, provisioning, and build/start-arg handling those providers build on, and does not register a backend itself.

## What it provides

- Each VPS runs exactly one Docker container. The VPS stays running at all times; `stop`/`start` operates on the container, and `destroy` removes both the container and the VPS.
- mngr connects directly to the container's sshd port (default 2222) on the VPS's public IP, using key-based auth.
- Agent data lives under `host_dir` (default `/mngr`) inside the container, on a per-host volume that supports consistent snapshots.

## Configuration

The base config (`VpsDockerProviderConfig`) provides these settings; each provider adds its own (see that provider's README):

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

## Build and start args

Build args (`-b`) serve two purposes: VPS provisioning and Docker image building.

**Provider-specific args** use a per-provider prefix (`--aws-`, `--vultr-`, `--ovh-`) and are consumed by the provider:

```
--<provider>-region=REGION       # Cloud region (aws / vultr / ovh)
--<provider>-instance-type=TYPE  # AWS only — EC2 instance type
--<provider>-plan=PLAN           # Vultr / OVH plan
--ovh-datacenter=DC              # OVH-specific alias for --ovh-region=
--git-depth=N                    # git clone depth of the local mngr build context
```

**All other build args** are passed through to `docker build` on the VPS:

```
--file=Dockerfile     # Use a specific Dockerfile
.                     # Build context (local directory, uploaded to VPS)
```

**Example** — create a host with a custom Dockerfile on a specific Vultr plan:

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
| `create` | Provision VPS, install Docker, create the per-host volume, run the container, set up SSH |
| `stop` | `docker stop` the container. VPS keeps running. |
| `start` | `docker start` the container. Wait for SSH. |
| `destroy` | Remove the container and per-host volume, destroy the VPS, clean up SSH keys |
| idle timeout | `docker stop` the container. VPS keeps running. |

## Limitations

Hosts created on an older on-disk volume layout cannot be discovered or managed after upgrade. Destroy and recreate them.
