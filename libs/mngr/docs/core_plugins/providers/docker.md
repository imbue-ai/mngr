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
default_start_args = ["--cpus=2", "--memory=4g"]  # Default docker run flags
default_idle_timeout = 800
```

Set `host` to connect to a remote Docker daemon (e.g., `ssh://user@server` or `tcp://host:2376`).

## Limitations

- Tags are immutable after container creation (stored as Docker labels)
- Volume mounts are not captured in snapshots
