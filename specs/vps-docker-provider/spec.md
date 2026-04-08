# VPS Docker Provider -- Detailed Spec

## Motivation

mngr currently supports three provider backends for running agents:

- **local**: Agents run directly on the user's machine. No isolation, no remote access.
- **docker**: Agents run in Docker containers on the user's machine (or a Docker host reachable via `DOCKER_HOST`). Good isolation, but the user must have Docker running locally or manage their own Docker host.
- **modal**: Agents run in Modal cloud sandboxes. Great isolation and zero infrastructure management, but only works with Modal (a specific managed service), and Modal is not available in all regions and doesn't support all hardware configurations.

There is a large gap between "run Docker locally" and "use Modal". Many users have access to VPS providers (Vultr, DigitalOcean, Hetzner, Linode, AWS Lightsail, etc.) and want to run agents on cheap cloud VMs without being locked into Modal. They want the isolation benefits of Docker containers but on infrastructure they control.

The goal of this project is to create a generic VPS Docker provider layer that provisions a VPS from any cloud provider, installs Docker on it, and runs agents inside a Docker container on that VPS. The VPS is accessed purely via SSH, and the Docker container inside is accessed via SSH to the VPS's public IP on a dedicated port. This gives users Modal-like convenience (one command to create remote agents) on any VPS provider.

We start with **Vultr** as the first concrete implementation because it has a simple API and cheap instances, but the abstraction layer is designed so that adding DigitalOcean, Hetzner, etc. is just implementing a small set of abstract methods.

### Single mode of operation

The VPS stays running at all times; the Docker container is the "host" from mngr's perspective. Stopping the host stops the container; the VPS keeps running. Starting the host starts the container back up. When the host is destroyed (no more agents need it), the VPS instance is destroyed entirely.

This model uses Docker purely as a consistent provisioning mechanism -- it ensures every host gets the same environment regardless of the underlying VPS OS. The 1:1 mapping (one VPS runs exactly one Docker container) keeps lifecycle management simple.

## Architecture

```
User Machine                              VPS (Vultr/DO/Hetzner/...)
+------------------+                      +-------------------------------+
|                  |   SSH (port 22)      |  VPS OS (Debian/Ubuntu)       |
|  mngr CLI        | ------------------> |  (Docker commands over SSH)   |
|                  |                      |  Docker Engine                |
|  ~/.mngr/        |   SSH (port 2222)   |  +-------------------------+ |
|    profile/      | ------------------> |  | Container (sshd)        | |
|      providers/  |   direct to         |  |                         | |
|        vultr/    |   VPS:2222          |  |  /mngr/ (host_dir)     | |
|          keys/   |                      |  |    agents/              | |
|                  |                      |  |    activity/            | |
+------------------+                      |  |    commands/            | |
                                          |  +-------------------------+ |
                                          |                               |
                                          |  Docker named volume          |
                                          |  (persistent host_dir data)   |
                                          |                               |
                                          |  State container + volume     |
                                          |  (host records, agent data)   |
                                          +-------------------------------+
```

Key architectural decisions:

- **1:1 mapping**: Each VPS runs exactly one Docker container. No multi-container support. Docker is used purely for consistent provisioning, not for multiplexing.
- **SSH host keys injected at creation**: SSH host keys are generated locally and injected into the VPS via cloud-init `user_data`. The public key is added to local `known_hosts` immediately. No TOFU (trust-on-first-use) needed.
- **Docker commands over SSH**: All Docker commands on the VPS are executed via SSH (`ssh user@vps docker ...`), not via Docker SDK remote host. This keeps the attack surface minimal and reuses the same SSH connection we already have.
- **Direct SSH to container**: Once the container is running sshd, mngr connects directly to the VPS's public IP on port 2222 (`ssh -i <container_key> -p 2222 root@<vps-ip>`). The container's sshd port is mapped to `0.0.0.0:2222` on the VPS. This is simpler than ProxyJump (fewer SSH hops, no proxy command configuration) and the container has its own SSH key authentication for security.
- **State on the VPS**: All host records and agent data are stored on a Docker state volume on the VPS itself, following the same pattern as the existing Docker provider (state container + named volume). This keeps all infrastructure state self-contained with the VPS.
- **Host volume**: A separate Docker named volume stores the host_dir data, making it persistent across container stop/start cycles.

## Package Structure

Two new packages:

```
libs/mngr_vps_docker/                    # Base classes + shared infrastructure
  pyproject.toml
  imbue/mngr_vps_docker/
    __init__.py                          # hookimpl marker
    config.py                            # VpsDockerProviderConfig
    errors.py                            # VpsDockerError hierarchy
    primitives.py                        # VPS-specific primitives (VpsInstanceId, etc.)
    vps_client.py                        # Abstract VpsClientInterface
    instance.py                          # VpsDockerProvider implementation
    host_store.py                        # HostRecord, VpsDockerHostStore
    docker_over_ssh.py                   # Run Docker commands on the VPS via SSH
    cloud_init.py                        # Generate cloud-init user_data scripts
    testing.py                           # Test utilities

libs/mngr_vultr/                         # Vultr-specific implementation
  pyproject.toml
  imbue/mngr_vultr/
    __init__.py                          # hookimpl marker
    config.py                            # VultrProviderConfig
    backend.py                           # Plugin registration
    client.py                            # VultrVpsClient (implements VpsClientInterface)
    testing.py                           # Test utilities
```

### Dependency relationships

```
mngr_vultr -> mngr_vps_docker -> mngr (core)
                                  ^
                                  |
                          (reuses providers/ssh_utils,
                           providers/ssh_host_setup,
                           providers/base_provider)
```

`mngr_vps_docker` is a library -- it has no entry points and registers no backends. Only `mngr_vultr` (and future `mngr_digitalocean`, etc.) register entry points.

## VPS Client Interface

The core abstraction that concrete VPS providers implement. This is a pure API client -- no Docker, SSH setup, or mngr-specific logic. The Vultr implementation makes raw HTTP calls to the Vultr API v2 (no third-party SDK).

```python
class VpsClientInterface(MutableModel, ABC):
    """Abstract interface for VPS provider API operations."""

    @abstractmethod
    def create_instance(
        self,
        label: str,
        region: str,
        plan: str,
        os_id: int,
        user_data: str,
        ssh_key_ids: Sequence[str],
        tags: Sequence[str],
    ) -> VpsInstanceId:
        """Provision a new VPS instance. Returns the instance ID."""
        ...

    @abstractmethod
    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        """Permanently destroy a VPS instance."""
        ...

    @abstractmethod
    def get_instance_status(self, instance_id: VpsInstanceId) -> VpsInstanceStatus:
        """Get the current status of a VPS instance."""
        ...

    @abstractmethod
    def get_instance_ip(self, instance_id: VpsInstanceId) -> str:
        """Get the main IPv4 address of a VPS instance."""
        ...

    @abstractmethod
    def wait_for_instance_active(
        self, instance_id: VpsInstanceId, timeout_seconds: float = 300.0
    ) -> str:
        """Poll until instance is active and return its IP address."""
        ...

    @abstractmethod
    def create_snapshot(self, instance_id: VpsInstanceId, description: str) -> VpsSnapshotId:
        """Create a snapshot of the instance's disk."""
        ...

    @abstractmethod
    def delete_snapshot(self, snapshot_id: VpsSnapshotId) -> None:
        """Delete a snapshot."""
        ...

    @abstractmethod
    def list_snapshots(self) -> list[VpsSnapshotInfo]:
        """List all snapshots owned by this account."""
        ...

    @abstractmethod
    def upload_ssh_key(self, name: str, public_key: str) -> str:
        """Upload an SSH public key. Returns the key ID."""
        ...

    @abstractmethod
    def delete_ssh_key(self, key_id: str) -> None:
        """Delete an SSH key by its ID."""
        ...
```

The interface is deliberately minimal. Each method maps to a single API call. The VPS Docker provider layer (in `instance.py`) composes these into higher-level operations.

### Vultr Implementation

`VultrVpsClient` implements `VpsClientInterface` using raw HTTP calls to the Vultr API v2 (`https://api.vultr.com/v2`). Authentication is via `Bearer` token in the `Authorization` header.

Key API endpoints used:
- `POST /instances` -- create instance (with `user_data`, `sshkey_id`, `tag`, `label`)
- `DELETE /instances/{id}` -- destroy instance
- `GET /instances/{id}` -- get instance status and IP
- `POST /snapshots` -- create snapshot (body: `instance_id`)
- `DELETE /snapshots/{id}` -- delete snapshot
- `GET /snapshots` -- list snapshots
- `POST /ssh-keys` -- upload SSH key
- `DELETE /ssh-keys/{id}` -- delete SSH key

### VPS Instance Status

```python
class VpsInstanceStatus(UpperCaseStrEnum):
    """Status of a VPS instance as reported by the provider API."""

    PENDING = auto()     # Being provisioned
    ACTIVE = auto()      # Running
    HALTED = auto()      # Powered off, disk preserved
    DESTROYING = auto()  # Being deleted
    UNKNOWN = auto()     # Unrecognized status
```

## Cloud-Init and VPS Provisioning

When a VPS is created, we pass a cloud-init `user_data` script that:

1. **Injects the SSH host key**: We generate an Ed25519 host keypair locally and embed the private key in the cloud-init script. This means we know the host key before the VPS boots, so we can add it to `known_hosts` immediately -- no TOFU prompts.
2. **Disables password authentication**: Ensures only key-based SSH access.
3. **Installs Docker**: Runs the official Docker install script (`get.docker.com`).
4. **Signals readiness**: Creates a marker file (`/var/run/mngr-ready`) after Docker is installed.

```yaml
#cloud-config
ssh_deletekeys: true
ssh_keys:
  ed25519_private: |
    <generated-private-key>
  ed25519_public: <generated-public-key>
runcmd:
  - apt-get update -qq
  - apt-get install -y -qq curl ca-certificates
  - curl -fsSL https://get.docker.com | sh
  - touch /var/run/mngr-ready
```

The SSH *client* key (for mngr to authenticate to the VPS) is uploaded to the VPS provider's SSH key store and referenced by ID in the create call. This is more reliable than injecting it via cloud-init, because the provider ensures the key is in `authorized_keys` even before cloud-init runs.

### VPS SSH Key Lifecycle

```
1. mngr generates RSA keypair locally (or loads existing one)
2. mngr uploads public key to Vultr SSH key store -> gets key_id
3. mngr creates VPS with ssh_key_ids=[key_id]
4. Vultr injects public key into root's authorized_keys
5. mngr can SSH into VPS immediately after boot
6. On destroy, mngr deletes the SSH key from Vultr
```

The SSH keys are stored at `~/.mngr/profile/providers/vultr/<instance_name>/keys/`:
- `vps_ssh_key` / `vps_ssh_key.pub` -- For authenticating to the VPS itself
- `container_ssh_key` / `container_ssh_key.pub` -- For authenticating to the Docker container (separate key for defense in depth)
- `host_key` / `host_key.pub` -- Ed25519 host key injected into VPS via cloud-init
- `container_host_key` / `container_host_key.pub` -- Ed25519 host key for the container's sshd
- `vps_known_hosts` -- Known hosts file for VPS connections
- `container_known_hosts` -- Known hosts file for container connections

### Known Hosts Management

When a VPS is created:
1. Generate host keypair locally
2. Inject private key into VPS via cloud-init `ssh_keys`
3. Add `<vps-ip> <host-public-key>` to `vps_known_hosts`

When a VPS is destroyed:
1. Remove entry from `vps_known_hosts`

The container gets its own known_hosts file (`container_known_hosts`) with an entry for `[<vps-ip>]:2222 <container-host-public-key>`. Since mngr connects directly to the VPS IP on port 2222, the known_hosts entry uses the VPS IP as the hostname.

## Docker Over SSH

All Docker commands on the VPS are executed by running `ssh user@vps docker <command>`. This module provides helpers for common operations:

```python
class DockerOverSsh:
    """Execute Docker commands on a remote VPS via SSH."""

    def __init__(
        self,
        vps_ip: str,
        ssh_user: str,
        ssh_key_path: Path,
        known_hosts_path: Path,
    ) -> None: ...

    def run(self, docker_args: Sequence[str], timeout_seconds: float = 60.0) -> str:
        """Run a docker command on the VPS and return stdout."""
        ...

    def run_container(
        self,
        image: str,
        name: str,
        ports: Mapping[int, int],
        volumes: Sequence[str],
        labels: Mapping[str, str],
        extra_args: Sequence[str],
        entrypoint_cmd: str,
    ) -> str:
        """Run a detached container. Returns container ID."""
        ...

    def stop_container(self, container_id_or_name: str, timeout_seconds: int = 10) -> None: ...
    def start_container(self, container_id_or_name: str) -> None: ...
    def remove_container(self, container_id_or_name: str, force: bool = False) -> None: ...
    def exec_in_container(self, container_id_or_name: str, command: str) -> str: ...
    def commit_container(self, container_id_or_name: str, image_name: str) -> str: ...
    def inspect_container(self, container_id_or_name: str) -> dict: ...
    def list_containers(self, labels: Mapping[str, str] | None = None) -> list[dict]: ...
    def pull_image(self, image: str) -> None: ...
    def build_image(self, tag: str, build_args: Sequence[str]) -> None: ...
    def create_volume(self, name: str) -> None: ...
    def remove_volume(self, name: str) -> None: ...
    def volume_exists(self, name: str) -> bool: ...
```

Each method constructs an SSH command like:
```bash
ssh -i <key> -o UserKnownHostsFile=<known_hosts> -o StrictHostKeyChecking=yes root@<vps-ip> docker <args>
```

The SSH connection uses `StrictHostKeyChecking=yes` because we pre-populate known_hosts.

## Container Setup

After the VPS is ready and Docker is installed, the provider:

1. **Creates the state volume and state container** on the VPS (same pattern as Docker provider -- a singleton Alpine container mounting a named volume for host records and agent data)
2. **Creates a Docker named volume** for host data: `mngr-host-vol-<host_id_hex>`
3. **Pulls or builds the base image** (default: `debian:bookworm-slim`, or user-specified)
4. **Runs the container** with:
   - Port 2222 mapped on the VPS's public interface (`-p 0.0.0.0:2222:22`)
   - The host volume mounted at `/mngr-vol`
   - Labels for discovery (`com.imbue.mngr.host-id`, etc.)
   - The container entrypoint: `trap 'exit 0' TERM; tail -f /dev/null & wait`
   - Any user-specified start_args
5. **Sets up SSH in the container** (via `docker exec`):
   - Installs required packages (openssh-server, tmux, curl, rsync, git, jq, etc.) using `build_check_and_install_packages_command` from `ssh_host_setup`
   - Configures SSH keys (container client key, container host key)
   - Symlinks `/mngr` to `/mngr-vol` (the volume mount point)
   - Starts sshd: `/usr/sbin/sshd -D -o MaxSessions=100 -p 22`
6. **Waits for container sshd** to be ready (via `wait_for_sshd` on `<vps_ip>:2222`)
7. **Creates a pyinfra Host** with direct SSH to `<vps_ip>:2222`
8. **Starts the activity watcher** and creates the shutdown script
9. **Writes HostRecord** to the state volume on the VPS

### Direct SSH to Container

To reach the container, mngr connects directly to the VPS's public IP on port 2222:

```
ssh -i <container_key> -p 2222 -o UserKnownHostsFile=<container_known_hosts> -o StrictHostKeyChecking=yes root@<vps-ip>
```

The pyinfra Host is created with these SSH settings:
```python
host_data = {
    "ssh_user": "root",
    "ssh_port": 2222,
    "ssh_key": str(container_key_path),
    "ssh_known_hosts_file": str(container_known_hosts_path),
    "ssh_strict_host_key_checking": "yes",
}
```

The container has its own SSH keypair (separate from the VPS keypair), so even though the port is publicly exposed, only holders of the container private key can authenticate.

## Lifecycle

| mngr operation | What happens |
|---|---|
| `create_host` | Provision VPS, install Docker, run container, setup SSH, write state to VPS |
| `stop_host` | `docker stop` the container on the VPS. VPS keeps running. |
| `start_host` | `docker start` the container. Re-setup SSH if needed. |
| `destroy_host` | `docker rm` the container. Delete host volume. Destroy the VPS instance. Delete SSH key from provider. |
| idle timeout | `docker stop` the container. VPS keeps running. |

### State Persistence

All host state is stored on the VPS itself via a Docker state volume, following the same pattern as the existing Docker provider:

1. **State volume + state container**: A singleton Alpine container per VPS, mounting a Docker named volume (`mngr-docker-state-<user_id>`). Host records and agent data are stored here as JSON files, accessed via `docker exec` into the state container.
2. **Host volume**: A separate Docker named volume (`mngr-host-vol-<host_id_hex>`) stores the host_dir data (agents, activity, commands). This is mounted into the agent container and persists across container stop/start.

Directory layout on the state volume:
```
host_state/
  <host_id>.json              # HostRecord (VPS instance ID, SSH info, certified data, snapshots)
  <host_id>/
    <agent_id>.json           # Persisted agent data for offline listing
```

This means all state is self-contained on the VPS. To list hosts, mngr SSHes to the VPS and reads from the state volume. For offline discovery when the container is stopped, the state volume is still accessible via the state container (which keeps running independently of the agent container).

### Snapshots

Snapshots use `docker commit` on the remote container (executed via SSH):

```bash
ssh root@<vps-ip> docker commit <container-id> mngr-snapshot-<host_id>-<timestamp>
```

The resulting image stays on the VPS's Docker image store. To restore from a snapshot, the old container is removed and a new one is created from the snapshot image (same pattern as Docker provider).

Snapshot images are local to the VPS. If the VPS is destroyed, snapshots are lost. This is acceptable since snapshots are primarily for quick rollbacks, not for long-term backup.

## Configuration

### VpsDockerProviderConfig (base)

```python
class VpsDockerProviderConfig(ProviderInstanceConfig):
    """Base configuration for VPS Docker providers."""

    host_dir: Path = Field(
        default=Path("/mngr"),
        description="Base directory for mngr data inside containers",
    )
    default_image: str = Field(
        default="debian:bookworm-slim",
        description="Default Docker image for containers",
    )
    default_idle_timeout: int = Field(
        default=800,
        description="Default idle timeout in seconds",
    )
    default_idle_mode: IdleMode = Field(
        default=IdleMode.IO,
        description="Default idle detection mode",
    )
    default_activity_sources: tuple[ActivitySource, ...] = Field(
        default_factory=lambda: tuple(ActivitySource),
        description="Default activity sources",
    )
    ssh_connect_timeout: float = Field(
        default=60.0,
        description="Timeout for SSH connections in seconds",
    )
    vps_boot_timeout: float = Field(
        default=300.0,
        description="Timeout for VPS to become active after provisioning in seconds",
    )
    docker_install_timeout: float = Field(
        default=300.0,
        description="Timeout for Docker installation on the VPS in seconds",
    )
    container_ssh_port: int = Field(
        default=2222,
        description="Port for sshd inside the Docker container (mapped to VPS localhost)",
    )
    default_start_args: tuple[str, ...] = Field(
        default=(),
        description="Default docker run arguments applied to all containers",
    )
```

### VultrProviderConfig

```python
class VultrProviderConfig(VpsDockerProviderConfig):
    """Configuration for the Vultr VPS Docker provider."""

    backend: ProviderBackendName = Field(
        default=ProviderBackendName("vultr"),
        description="Provider backend (always 'vultr' for this type)",
    )
    api_key: SecretStr | None = Field(
        default=None,
        description="Vultr API key. Falls back to VULTR_API_KEY env var.",
    )
    default_region: str = Field(
        default="ewr",
        description="Default Vultr region (e.g., 'ewr' for New Jersey)",
    )
    default_plan: str = Field(
        default="vc2-1c-1gb",
        description="Default Vultr plan (e.g., 'vc2-1c-1gb' for 1 CPU, 1GB RAM)",
    )
    default_os_id: int = Field(
        default=2136,
        description="Default Vultr OS ID (2136 = Debian 12 x64)",
    )
```

### Build Args

Build args (`-b`) serve two purposes: VPS provisioning and Docker image building. VPS-specific args use the `--vps-` prefix and are consumed by the provider. All other args are passed through to `docker build` on the VPS (the build context is uploaded via rsync).

VPS providers must not use flags that conflict with Docker build flags. All VPS-specific flags must use the `--vps-` prefix.

```
--vps-region=ewr      # Vultr region (consumed by provider)
--vps-plan=vc2-2c-4gb # Vultr plan (consumed by provider)
--vps-os=2136         # Vultr OS ID (consumed by provider)
--file=Dockerfile     # Passed to docker build on VPS
.                     # Build context (uploaded to VPS, passed to docker build)
```

Example:
```bash
mngr create my-agent --provider vultr -b --vps-plan=vc2-2c-4gb -b --file=Dockerfile -b .
```

### Start Args

Start args are passed to `docker run` when creating the container:

```
--cpus=2              # CPU limit for container
--memory=4g           # Memory limit for container
-v /data:/data        # Extra volume mounts
--network host        # Network mode
```

These follow the same pattern as the Docker provider's start args.

## Host Records

Host records are stored on the VPS's state volume at `host_state/<host_id>.json`:

```python
class VpsHostConfig(HostConfig):
    """VPS-specific host configuration stored in the host record."""

    vps_instance_id: VpsInstanceId = Field(description="Provider-specific VPS instance ID")
    region: str = Field(description="Region where the VPS was created")
    plan: str = Field(description="VPS plan (CPU/RAM specification)")
    os_id: int = Field(description="OS image ID used to create the VPS")
    start_args: tuple[str, ...] = Field(default=(), description="Docker run arguments for replay on snapshot restore")
    image: str | None = Field(default=None, description="Docker image used for the container")
    container_name: str = Field(description="Docker container name on the VPS")
    volume_name: str = Field(description="Docker volume name on the VPS")
    vps_ssh_key_id: str | None = Field(default=None, description="Vultr SSH key ID (for cleanup on destroy)")

class VpsDockerHostRecord(FrozenModel):
    """Host metadata stored on the VPS state volume."""

    certified_host_data: CertifiedHostData
    vps_ip: str | None = Field(default=None, description="Current IP address of the VPS")
    ssh_host_public_key: str | None = Field(default=None, description="VPS SSH host public key")
    container_ssh_host_public_key: str | None = Field(default=None, description="Container SSH host public key")
    config: VpsHostConfig | None = Field(default=None, description="VPS and container configuration")
    container_id: str | None = Field(default=None, description="Docker container ID")
```

The host store follows the same `DockerHostStore` pattern -- reads and writes to the state volume via `docker exec` on the state container. This reuses the existing `DockerHostStore` class (or a parallel implementation backed by `DockerOverSsh` instead of the local Docker SDK).

## Discovery and Listing

### Online hosts (VPS running, container running)

1. SSH to VPS, read state volume for all HostRecords
2. Check container status via `docker inspect` over SSH
3. If container is running, SSH into it (via ProxyJump) to read agent data
4. Build HostDetails and AgentDetails as usual

### Offline hosts (container stopped, VPS still running)

1. SSH to VPS, read state volume for all HostRecords
2. Read persisted agent data from state volume
3. Build OfflineHost from certified data in the HostRecord

Since the VPS is always running, the state volume is always accessible. There is no truly "offline" case -- even when the agent container is stopped, we can SSH to the VPS and read the state container.

### VPS unreachable

If the VPS cannot be reached (network issue, etc.), `on_connection_error` is called and the host is reported as unreachable. Since all state lives on the VPS, we cannot show any cached data in this case. The Vultr API can be queried to confirm the VPS exists and is running, but detailed host/agent data is unavailable until SSH connectivity is restored.

## Idle Detection and Shutdown

Idle detection works identically to the Docker provider:

1. Activity watcher runs inside the container, monitoring file mtimes in `host_dir/activity/`
2. When idle timeout is reached, the watcher calls `host_dir/commands/shutdown.sh`
3. The shutdown script sends SIGTERM to PID 1 in the container, which causes the container to stop
4. The VPS keeps running

```bash
#!/bin/bash
kill -TERM 1
```

This is the same shutdown mechanism as the Docker provider. The VPS remains up so that the state volume is still accessible for discovery and listing.

## Plugin Registration

### Entry Points

```toml
# libs/mngr_vultr/pyproject.toml
[project.entry-points.mngr]
vultr = "imbue.mngr_vultr.backend"
```

### Backend Registration

```python
# libs/mngr_vultr/imbue/mngr_vultr/backend.py

@hookimpl
def register_provider_backend() -> tuple[type[ProviderBackendInterface], type[ProviderInstanceConfig]]:
    return (VultrProviderBackend, VultrProviderConfig)
```

`VultrProviderBackend` implements `ProviderBackendInterface`:
- `get_name()` returns `ProviderBackendName("vultr")`
- `get_description()` returns `"Runs agents in Docker containers on Vultr VPS instances"`
- `get_config_class()` returns `VultrProviderConfig`
- `get_build_args_help()` returns help text for `--region`, `--plan`, `--os`, `--file`, `--image`
- `get_start_args_help()` returns `"Start args are passed directly to 'docker run'"`
- `build_provider_instance()` creates a `VpsDockerProvider` with a `VultrVpsClient`

## Error Handling

```python
class VpsDockerError(MngrError):
    """Base error for VPS Docker provider operations."""

class VpsProvisioningError(VpsDockerError):
    """Failed to provision a VPS instance."""

class VpsConnectionError(VpsDockerError):
    """Failed to connect to the VPS via SSH."""

class DockerNotInstalledError(VpsDockerError):
    """Docker is not installed or not running on the VPS."""

class ContainerSetupError(VpsDockerError):
    """Failed to set up the Docker container."""

class VpsApiError(VpsDockerError):
    """Error from the VPS provider API."""
```

## Security Considerations

1. **SSH key isolation**: Each provider instance gets its own SSH keypair. The VPS key and container key are separate -- compromising the container key doesn't grant VPS-level access.
2. **Container port binding**: The container's sshd port (2222) is bound to `0.0.0.0` on the VPS and accessible directly. Authentication requires the container SSH private key (separate from the VPS key). This is simpler than a ProxyJump setup and avoids double-hop latency.
3. **Cloud-init host keys**: SSH host keys are generated locally and injected via cloud-init. This prevents MITM attacks during initial connection.
4. **No API credentials in containers**: VPS provider API keys never enter the container. The container is treated as untrusted, same as Modal sandboxes.
5. **Firewall**: The VPS should have a firewall that allows SSH (port 22) and the container SSH port (default 2222) inbound. Docker's default bridge network provides container isolation.

## Process Lifecycle

### Host Creation

1. Generate or load SSH keypairs (VPS client key, container client key, VPS host key, container host key)
2. Upload VPS client public key to Vultr API
3. Generate cloud-init user_data with VPS host key
4. Create VPS via Vultr API (region, plan, OS, user_data, ssh_key_id)
5. Wait for VPS to become active (poll API, timeout 300s)
6. Add VPS IP + host key to `vps_known_hosts`
7. Wait for cloud-init to complete (poll via SSH for `/var/run/mngr-ready`, timeout 300s)
8. Create state volume and state container on VPS (via Docker over SSH)
9. Create host volume on VPS (`mngr-host-vol-<host_id_hex>`)
10. Pull or build Docker image on VPS
11. Run container with host volume mount, port mapping, labels
12. Set up SSH in container (install packages, configure keys, start sshd) via `docker exec`
13. Add container host key to `container_known_hosts`
14. Wait for container sshd (direct SSH to `<vps_ip>:2222`, timeout 60s)
15. Create pyinfra Host with direct SSH to `<vps_ip>:2222`
16. Set up activity watcher and shutdown script
17. Write HostRecord to state volume on VPS
18. Return Host object

### Host Stop

1. Persist agent data to state volume on VPS
2. `docker stop` the container on VPS (via SSH)
3. Update HostRecord on state volume with STOPPED state

### Host Start

1. SSH to VPS, read HostRecord from state volume
2. `docker start` the container on VPS (via SSH)
3. Wait for container sshd
4. Re-create pyinfra Host
5. Return Host object

### Host Destroy

1. `docker stop` and `docker rm` the container
2. `docker volume rm` the host volume
3. Delete HostRecord from state volume
4. Destroy the VPS instance via provider API
5. Delete SSH key from Vultr API
6. Clean up local known_hosts entries and key files

## Capability Properties

- `supports_snapshots` = `True`
- `supports_shutdown_hosts` = `True`
- `supports_volumes` = `True`
- `supports_mutable_tags` = `False` (tags stored in host record, not mutable on VPS)

## Changes to Existing Code

### No changes to mngr core

The VPS Docker provider is entirely in new packages (`mngr_vps_docker` and `mngr_vultr`). It reuses existing infrastructure from mngr core:

- `ssh_utils.py`: `generate_ssh_keypair()`, `generate_ed25519_host_keypair()`, `load_or_create_ssh_keypair()`, `load_or_create_host_keypair()`, `add_host_to_known_hosts()`, `wait_for_sshd()`, `create_pyinfra_host()`
- `ssh_host_setup.py`: `build_check_and_install_packages_command()`, `build_configure_ssh_command()`, `build_add_known_hosts_command()`, `build_add_authorized_keys_command()`, `build_start_activity_watcher_command()`
- `base_provider.py`: `BaseProviderInstance` base class
- `config/data_types.py`: `ProviderInstanceConfig`, `MngrContext`
- `interfaces/`: `ProviderBackendInterface`, `ProviderInstanceInterface`, `HostInterface`, `OnlineHostInterface`
- `hosts/host.py`: `Host` class
- `hosts/offline_host.py`: `OfflineHost` class
- `interfaces/data_types.py`: `CertifiedHostData`, `HostConfig`, `SnapshotRecord`, `HostLifecycleOptions`

### Docker provider patterns reused

The following patterns from the Docker provider are closely mirrored:

- **State container + state volume**: Same approach (singleton Alpine container with named volume), but all Docker commands go through `DockerOverSsh` instead of the local Docker SDK.
- **Host volume**: Same concept (per-host named volume, symlinked to host_dir).
- **Container entrypoint**: Same pattern (`trap 'exit 0' TERM; tail -f /dev/null & wait`).
- **DockerHostStore**: The VPS Docker provider can either import and reuse `DockerHostStore` (parameterized with a `Volume` implementation that works via `DockerOverSsh`) or create a parallel implementation. Reusing `DockerHostStore` directly is preferred if the `Volume` abstraction is clean enough.
- **Container labels**: Same labeling scheme (`com.imbue.mngr.host-id`, `com.imbue.mngr.host-name`, etc.).

## Open Questions

1. **Cloud-init vs startup script for Docker installation**: Cloud-init is standardized and portable across VPS providers, but adds a few seconds to boot. Vultr also supports "startup scripts" which may run earlier. **Recommendation**: Use cloud-init (`user_data`) for portability. The extra boot time is acceptable.

2. **Reusing `DockerHostStore` vs new implementation**: The existing `DockerHostStore` takes a `Volume` interface. If we implement `Volume` on top of `DockerOverSsh` (exec into the state container via SSH), we can reuse `DockerHostStore` directly. This is the preferred approach but depends on whether the `Volume` abstraction is clean enough to work over SSH. **Recommendation**: Implement a `RemoteDockerVolume` that adapts `DockerOverSsh` to the `Volume` interface, then pass it to `DockerHostStore`.

3. **VPS cleanup when unreachable**: If the VPS becomes permanently unreachable (e.g., provider outage), the user needs a way to clean up the local key files and known_hosts entries. `mngr gc` should detect VPSes that no longer exist in the provider API and clean up their local artifacts. **Recommendation**: Handle this in `mngr gc` or `mngr cleanup`.
