# Docker Provider

The Docker provider creates agents in Docker containers with SSH access. Each container runs sshd and is accessed via pyinfra's SSH connector, following the same pattern as the Modal provider.

## Usage

```bash
mngr create my-agent@.docker
```

## Build Arguments

Build arguments are passed directly to `docker build`. Use `-b` (or `--build-arg`) to specify them:

```bash
# Build from a Dockerfile
mngr create my-agent@.docker -b --file=./Dockerfile -b .

# Build with no cache
mngr create my-agent@.docker -b --file=./Dockerfile -b --no-cache -b .

# Build with build-time variables
mngr create my-agent@.docker -b --build-arg=MY_VAR=value -b --file=./Dockerfile -b .
```

Run `docker build --help` for the full list of supported flags.

## Start Arguments

Start arguments are passed directly to `docker run` for container resource limits, networking, volumes, and other runtime configuration. Use `-s` (or `--start-arg`):

```bash
# Set CPU and memory limits
mngr create my-agent@.docker -s --cpus=4 -s --memory=16g

# GPU access
mngr create my-agent@.docker -s --gpus=all

# Mount a volume
mngr create my-agent@.docker -s -v=/host/data:/container/data

# Attach to a network
mngr create my-agent@.docker -s --network=my-network

# Publish an additional port
mngr create my-agent@.docker -s -p=8080:80
```

Run `docker run --help` for the full list of supported flags.

## Size: CPU and memory caps

A container's size is its `--cpus` and `--memory` caps. They are chosen at create time (through `-s` flags, or the provider's `default_cpus` / `default_memory` settings below) and recorded with the host, so `mngr list` reports them as `host.resource.cpu.count` and `host.resource.memory_gb` whether the container is running or stopped. A dimension with no cap reports the daemon machine's total for it, since that is what an uncapped container can use. Disk is not reported: the host volume is a subpath of a shared named volume with no quota of its own.

Whenever mngr sets a memory cap it also sets `--memory-swap` to the same value, so a container at its limit is shed by the OOM killer instead of swapping the machine to a halt. A swap setting you passed yourself (`-s --memory-swap=6g`, or `-s --memory-swap=-1` for unlimited swap) is kept as recorded, and docker's `0` spellings (`--cpus=0`, `--memory=0`) read as uncapped.

To change a host's size after creation:

```bash
mngr docker resize my-host --cpus 4 --memory 8g
```

This rewrites the caps recorded for the host and applies them to its container with `docker update`, running or stopped; a dimension you do not pass keeps its value, and a value docker refuses (for example more CPUs than the daemon has) is reported as an error and changes nothing. The cgroup caps take effect at once. What a gVisor (runsc) container reports as its memory total in `/proc/meminfo` follows only after a restart, so restart the host if something inside it sizes itself from that. See [`mngr docker`](../../commands/secondary/docker.md).

## Snapshots

Docker containers support snapshots via `docker commit`:

```bash
# Create a snapshot
mngr snapshot create my-agent

# List snapshots
mngr snapshot list my-agent
```

Snapshots capture the container's filesystem layers. Volume mounts are not included in snapshots.

## Stop and Start

Unlike Modal, Docker supports native stop/start. Stopping a container preserves its filesystem state:

```bash
# Stop an agent (container filesystem is preserved)
mngr stop my-agent

# Start the stopped agent (container filesystem state is restored)
mngr start my-agent
```

## Tags

Tags are stored as Docker container labels and are immutable after creation. Set tags when creating a host:

```bash
mngr create my-agent@.docker --host-label env=test --host-label team=infra
```

Attempting to modify tags after creation will produce an error.

## Configuration

Configure the Docker provider in your mngr config file:

```toml
[providers.docker]
backend = "docker"
host = ""                    # Docker host URL (empty = local daemon)
default_image = "debian:bookworm-slim"
default_cpus = 2                  # CPU cap for new containers (docker run --cpus)
default_memory = "4g"             # Memory cap for new containers (docker run --memory, swap capped at the same value)
default_start_args = ["--tmpfs", "/run"]  # Extra docker run flags for every container
default_idle_timeout = 800
# ssh_bind_address = "0.0.0.0"  # Host IP the container's published SSH port binds to
```

Set `host` to connect to a remote Docker daemon (e.g., `ssh://user@server` or `tcp://host:2376`).

`default_cpus` and `default_memory` are rendered before `default_start_args` and any `-s` flags, and docker keeps the last spelling of a repeated flag, so either of those can override them per container. A `default_cpus` above the daemon's CPU count (which docker refuses) is clamped to it with a warning.

### SSH port binding

Each container's sshd is published on a random host port. By default that port binds to `127.0.0.1` when the daemon is local (`host` empty or a `unix://` socket), so the container is not reachable from the machine's LAN, and to all interfaces when the daemon is remote (`ssh://` or `tcp://`), since mngr reaches remote containers via the daemon's hostname. Set `ssh_bind_address` to an IP address to override either default, for example `"0.0.0.0"` to expose local containers to the LAN. On a local daemon mngr then connects to that address (a wildcard bind is reached via `127.0.0.1`), so it must be an IPv4 address. With a remote daemon, a loopback address makes creating a container (or restoring one from a snapshot) fail, because mngr could not reach it; other commands are unaffected, so a disabled or unused docker provider cannot break them.

The binding is fixed when the container is created: existing containers keep theirs across stop/start, and pick up the current setting on a snapshot restore or recreate.

## Limitations

- Tags are immutable after container creation (stored as Docker labels)
- Volume mounts are not captured in snapshots
