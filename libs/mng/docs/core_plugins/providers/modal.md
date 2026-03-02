# Modal Provider

The Modal provider creates agents in [Modal](https://modal.com) sandboxes. Each sandbox runs sshd and is accessed via SSH.

## Usage

```bash
mng create my-agent --in modal
```

## Build Arguments

Build arguments configure the Modal sandbox. Pass them using `-b` or `--build-args`:

```bash
# Key-value format (recommended)
mng create my-agent --in modal -b gpu=h100 -b cpu=2 -b memory=8

# Flag format (also supported)
mng create my-agent --in modal -b --gpu=h100 -b --cpu=2

# Bulk format
mng create my-agent --in modal --build-args "gpu=h100 cpu=2 memory=8"
```

### Available Build Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `gpu` | GPU type (e.g., `h100`, `a100`, `t4`) | None |
| `cpu` | Number of CPU cores | 1.0 |
| `memory` | Memory in GB | 1.0 |
| `image` | Base container image | debian:bookworm-slim |
| `timeout` | Sandbox timeout in seconds | 900 (15 minutes) |
| `region` | Region to run the sandbox in (e.g., `us-east`, `us-west`, `eu-west`) | auto |
| `context-dir` | Build context directory for Dockerfile COPY/ADD instructions | Dockerfile's directory |
| `secret` | Environment variable name to pass as a secret during image build (can be specified multiple times) | None |
| `cidr-allowlist` | Restrict network access to the specified CIDR range (can be specified multiple times) | None |
| `file` | Path to a Dockerfile for building a custom image | None |
| `offline` | Block all outbound network access from the sandbox | off |
| `volume` | Mount a persistent Modal Volume (format: `name:/path`, can be specified multiple times) | None |
| `docker-build-arg` | Override a Dockerfile ARG default value (format: `KEY=VALUE`, can be specified multiple times) | None |

### Examples

```bash
# Create with an H100 GPU
mng create my-agent --in modal -b gpu=h100

# Create with more resources
mng create my-agent --in modal -b cpu=4 -b memory=16

# Create with custom image and longer timeout
mng create my-agent --in modal -b image=python:3.11-slim -b timeout=3600

# Create with network restricted to specific CIDR ranges
mng create my-agent --in modal -b cidr-allowlist=203.0.113.0/24 -b cidr-allowlist=10.0.0.0/8

# Create with no outbound network access
mng create my-agent --in modal -b offline
```

### Restricting Network Access

The `--offline` and `--cidr-allowlist` build arguments restrict **outbound** network access from the sandbox. Inbound connections (including the SSH tunnel that mng uses to communicate with the sandbox) are unaffected.

Note: Modal's SDK also offers a `block_network` parameter that blocks all network access (both inbound and outbound), but it is incompatible with the SSH tunneling that mng requires. If Modal adds support for combining `block_network` with port tunneling in the future, we can expose that as a stronger isolation option.

When using `--offline` or `--cidr-allowlist`, the sandbox cannot install packages at runtime (e.g., via `apt-get`), so you must provide a pre-configured image that includes all required packages.

At minimum, the image must include `openssh-server`, `tmux`, `curl`, `rsync`, `git`, and `jq`. For example:

```dockerfile
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-server tmux curl rsync git jq \
    && rm -rf /var/lib/apt/lists/*
```

Then use it with:

```bash
mng create my-agent --in modal -b file=./Dockerfile -b offline
```

### Using Secrets During Image Build

The `secret` build argument allows passing environment variables as secrets to the image build process. This is useful for installing private packages or accessing authenticated resources during the Dockerfile build:

```bash
# Pass a single secret
mng create my-agent --in modal -b file=./Dockerfile -b secret=NPM_TOKEN

# Pass multiple secrets
mng create my-agent --in modal -b file=./Dockerfile -b secret=NPM_TOKEN -b secret=GH_TOKEN
```

In your Dockerfile, access the secret using `--mount=type=secret`:

```dockerfile
FROM python:3.11-slim

# Install a private npm package using NPM_TOKEN
RUN --mount=type=secret,id=NPM_TOKEN \
    npm config set //registry.npmjs.org/:_authToken=$(cat /run/secrets/NPM_TOKEN) && \
    npm install -g @myorg/private-package

# Install a private pip package using GH_TOKEN
RUN --mount=type=secret,id=GH_TOKEN \
    pip install git+https://$(cat /run/secrets/GH_TOKEN)@github.com/myorg/private-repo.git
```

### Mounting Persistent Volumes

The `volume` build argument mounts a persistent [Modal Volume](https://modal.com/docs/guide/volumes) at a specified path inside the sandbox. Volumes persist across sandbox restarts and can be shared between sandboxes.

```bash
# Mount a single volume
mng create my-agent --in modal -b volume=my-data:/data

# Mount multiple volumes
mng create my-agent --in modal -b volume=cache:/cache -b volume=results:/results
```

The volume is created automatically if it does not already exist. Data written to the mount path is persisted on the volume and available the next time a sandbox mounts the same volume.

## Snapshots

Modal sandboxes support native filesystem snapshots. Snapshots are fast and fully incremental (only changes since the last snapshot are captured).

```bash
# Create a snapshot
mng snapshot create my-agent

# Create a named snapshot
mng snapshot create my-agent --name before-refactor

# List snapshots
mng snapshot list my-agent

# Destroy a specific snapshot
mng snapshot destroy my-agent --snapshot <id> --force

# Start from a snapshot (restores the sandbox state) [future]
mng start my-agent --snapshot <id>
```

Snapshots are stored as Modal images and persist even after the sandbox is terminated.

Snapshot consistency semantics are "hard power off": in-flight writes may not be captured. For databases or other stateful applications, this is usually fine since they're designed to survive power loss.

See [`mng snapshot`](../../commands/secondary/snapshot.md) for all options.

## Host Volume

By default, mng creates a persistent Modal Volume for each host's data directory. This volume stores logs, agent data, and other host state, making them accessible even when the host is offline (e.g., via `mng logs`).

You can disable this behavior by setting `is_host_volume_created = false` in your provider configuration:

```toml
[providers.modal]
backend = "modal"
is_host_volume_created = false
```

When `is_host_volume_created` is `false`:

- No persistent volume is created or mounted for the host directory
- The host directory is created as a regular directory on the sandbox filesystem
- The periodic volume sync process is not started
- Logs and other host data are only available while the host is online

This is useful when you don't need offline access to logs and want to avoid the overhead of volume management.

## Limitations

- Sandboxes have a maximum lifetime (timeout) after which they are automatically terminated by Modal. It is useful as a hard restriction on agent lifetime, but cannot be longer than 24 hours (currently)
- Sandboxes cannot be stopped and resumed directly. Instead, snapshots are used to preserve state before termination. Snapshots can be taken on-demand or via snapshot-on-idle (periodic snapshots [future])
