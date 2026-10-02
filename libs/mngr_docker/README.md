# mngr Docker Provider

Docker provider backend plugin for mngr. Runs agents in Docker containers (on a local or remote daemon) with SSH access.

## Prerequisites

- [Docker](https://docs.docker.com/engine/install/) with a reachable daemon: local Docker Desktop or Docker Engine, a remote daemon via `DOCKER_HOST`, or a configured Docker context. mngr resolves the daemon in the same order as the Docker CLI.
- Docker Engine 25.0 or newer for the isolated host-volume layout (`isolate_host_volumes = true`).

## Usage

```bash
# Install the plugin (the install script offers it when a Docker daemon is detected)
uv tool install imbue-mngr --with imbue-mngr-docker
# or, on an existing install:
mngr plugin add imbue-mngr-docker

# Create a container host
mngr create @.docker

# Build the container from your own Dockerfile
mngr create @.docker -b --file=./Dockerfile -b .

# Pass flags to docker run
mngr create @.docker -s --cpus=4 -s --memory=16g

# Change a running or stopped host's caps afterwards
mngr docker resize my-host --cpus 4 --memory 8g
```

Build arguments (`-b`) go straight to `docker build` and start arguments (`-s`) straight to `docker run`, so anything those CLIs support is available.

## Configuration

An example `[providers.docker]` block; every field is optional, and the caps shown are choices rather than the provider's defaults (new containers are uncapped unless you set them):

```toml
[providers.docker]
host = ""                        # Docker host URL (empty = local daemon; ssh://user@server, tcp://host:2376)
default_image = "debian:bookworm-slim"
default_cpus = 2                 # CPU cap for new containers (docker run --cpus)
default_memory = "4g"            # Memory cap for new containers; swap is capped at the same value
default_start_args = ["--tmpfs", "/run"]
default_idle_timeout = 800
isolate_host_volumes = true      # each container sees only its own host volume (Docker Engine >= 25)
# docker_runtime = "runsc"       # a registered alternative runtime, e.g. gVisor
```

The full option list, the sizing and snapshot behavior, and the SSH port-binding rules are in mngr's [docker provider reference](https://github.com/imbue-ai/mngr/blob/main/libs/mngr/docs/core_plugins/providers/docker.md); `mngr help docker_usage` walks through common setups.

## How it works

Each host is a container running sshd, reached over SSH like every other mngr host. Provider metadata (host records, persisted agent data, per-host volumes) lives on a Docker named volume that a small singleton state container serves, so several mngr clients can share one daemon. Snapshots are `docker commit`s; stop and start are native container stop and start. See [ARCHITECTURE.md](ARCHITECTURE.md) for the details.
