# Using Docker

Run coding agents in Docker containers. For general agent management, see [mngr create](../commands/primary/create.md). For the full list of Docker provider arguments, see the [Docker provider reference](../core_plugins/providers/docker.md).

## Prerequisites

- Docker installed and a reachable daemon (local Docker Desktop, a remote daemon via `DOCKER_HOST`, or a configured Docker context)
- mngr installed and working locally

The Docker client used by mngr resolves the daemon in the same order as the Docker CLI: `DOCKER_HOST`, then the active Docker context (from `~/.docker/config.json`), then the platform default. This means Docker Desktop on macOS works without extra configuration.

## Creating a local agent

From any git repo:

```bash
mngr create my-agent --provider docker
```

This builds a container, drops you into a tmux session, and gives you the same interactive experience as a local agent. Equivalently you can use the address form `mngr create my-agent@.docker`.

If you do not pass an image or a Dockerfile, mngr builds a default image from `debian:bookworm-slim` with the packages it needs (`openssh-server`, `tmux`, `curl`, `rsync`, `git`, `jq`, `xxd`, `ca-certificates`). For faster startup on repeated creates, supply your own image (see below) so these packages are pre-installed alongside your project's dependencies.

### Using a template

If your project has a Docker template defined in `.mngr/settings.toml`, you can use `-t my-docker` instead of passing flags manually:

```bash
mngr create my-agent -t my-docker
```

Example template:

```toml
[create_templates.my-docker]
provider = "docker"
agent_args = ["--dangerously-skip-permissions"]
pass_env = ["GH_TOKEN"]
extra_window = ["github_setup='ssh-keyscan github.com >> ~/.ssh/known_hosts && git remote set-url origin https://github.com/<org>/<repo>.git && gh auth setup-git'"]
```

The container is an isolated environment, so `--dangerously-skip-permissions` is reasonable for the container itself -- but credentials forwarded via `pass_env` (e.g. `GH_TOKEN`) can still be used by the agent without confirmation. Note also that the container can read/write any bind-mounted host paths you pass via `-s -v=...`, so do not rely on the container as a strong sandbox if you mount sensitive host directories.

See [Create Templates](../customization.md#create-templates) for the full set of template options.

## Resource limits, GPUs, networking, and volumes

Build arguments (`-b`) are passed to `docker build`. Start arguments (`-s`) are passed to `docker run`. Use start args for everything that affects how the container runs:

```bash
# CPU and memory limits
mngr create my-agent --provider docker -s --cpus=4 -s --memory=16g

# GPU access (requires the NVIDIA Container Toolkit)
mngr create my-agent --provider docker -s --gpus=all

# Bind-mount a host directory
mngr create my-agent --provider docker -s -v=/host/data:/container/data

# Attach to a Docker network
mngr create my-agent --provider docker -s --network=my-network

# Publish an extra port (the SSH port mngr uses is published automatically)
mngr create my-agent --provider docker -s -p=8080:80
```

You can set defaults that apply to every container in your config:

```toml
[providers.docker]
backend = "docker"
default_start_args = ["--cpus=2", "--memory=4g"]
```

Per-create `-s` flags are appended to the defaults; Docker uses the last occurrence when a flag is repeated.

## Custom images and Dockerfiles

There are three ways to control the base image:

1. **Use a pre-built image** -- set `default_image = "<ref>"` in your provider config. mngr pulls it on each create.
2. **Build from a Dockerfile** -- pass build args:
   ```bash
   mngr create my-agent --provider docker -b --file=./Dockerfile -b .
   ```
   Everything after `-b` is appended to `docker build -t <generated-tag>`. The trailing `-b .` is the build context. Add `-b --no-cache`, `-b --build-arg=KEY=VAL`, etc. as needed.
3. **Fall back to the mngr default Dockerfile** -- omit both. mngr warns and builds a minimal Debian image with the required packages.

Whatever image you provide must include (or be able to install at runtime) `openssh-server`, `tmux`, `curl`, `rsync`, `git`, `jq`, `xxd`, and `ca-certificates`. If you are running fully offline, pre-install them so the runtime install step is a no-op.

## Persistent host volume

By default, each host's `host_dir` (e.g. `/mngr`) is symlinked to a sub-folder of a shared Docker named volume (`<prefix>docker-state-<user_id>`). This volume is mounted into both the host container and a small singleton "state container" that mngr uses as a file server. Two consequences:

- **Offline access**: logs, agent data, and host metadata stay readable via `mngr events` and `mngr list` even after the container is stopped, because mngr reads them through the state container.
- **Shared daemon, multiple clients**: multiple mngr clients pointing at the same Docker daemon see the same hosts and agents (provided they use the same profile `user_id`). Different `user_id`s are isolated by separate state volumes.

You can disable this by setting `is_host_volume_created = false` in your provider config. The `host_dir` then lives on the container's overlay filesystem; it survives stop/start (Docker preserves the container filesystem) but is not accessible while the container is stopped.

User-supplied bind mounts (`-s -v=...`) are independent of the host volume. They are **not** captured in snapshots -- only the container's filesystem layers are.

## Getting changes back

To retrieve changes from the agent's container, either **let the agent push via git** or **use `mngr pull`**.

### Option A: Give the agent git credentials

If the agent has `GH_TOKEN` (via `pass_env` in a template or `--pass-env` on the CLI), it can `git push` directly.

### Option B: Use `mngr pull`

`mngr pull` transfers changes from the agent to your local machine without needing git credentials on the agent. It supports two sync modes:

**Pull git commits** (when the agent has committed its work):

```bash
mngr pull my-agent --sync-mode=git
```

This merges the agent's branch into your current local branch.

**Pull files** (default -- works for uncommitted changes and non-git-tracked files):

```bash
mngr pull my-agent
```

This uses rsync over SSH to sync the agent's working directory to your current directory. To preview what would be transferred first:

```bash
mngr pull my-agent --dry-run
```

You can also pull a specific subdirectory:

```bash
mngr pull my-agent:src ./local-src
```

To push local changes to the agent (e.g. a config file you edited locally):

```bash
mngr push my-agent:config ./config
```

See [mngr pull](../commands/primary/pull.md) and [mngr push](../commands/primary/push.md) for all options.

## Lifecycle and snapshots

`mngr connect`, `mngr message`, `mngr stop`, `mngr start`, `mngr destroy`, and `mngr list` all work the same as for local and Modal agents. The Docker-specific behavior:

- **Native stop/start.** Unlike Modal, Docker supports real `docker stop` / `docker start`. `mngr stop` stops the container (preserving its filesystem), and `mngr start` restarts the same container. `mngr destroy` removes the container permanently.
- **Idle detection still applies.** The default idle timeout is 800 seconds. When idle detection fires, the host is stopped (not destroyed), so `mngr start` will resume it. `--idle-mode disabled` keeps the container running indefinitely.
- **No forced lifetime cap.** Containers do not have a Modal-style maximum sandbox lifetime. They run until you stop them or idle detection stops them.
- **Snapshot before stop.** By default, `mngr stop` takes a snapshot via `docker commit` before stopping. If the container is later removed (rather than just stopped), `mngr start` will recreate it from the most recent snapshot.

You can also create named snapshots manually:

```bash
mngr snapshot create my-agent --name before-refactor
```

Snapshots are stored as Docker images (`mngr-snapshot:<host_id>-<name>`). They capture the container's filesystem layers but **not** the contents of any volumes -- bind mounts (`-s -v=...`), named volumes, or the shared host volume. When mngr restores a host from a snapshot, it re-mounts the same host volume sub-folder, so anything the agent wrote under `host_dir` (e.g. `/mngr`) reappears via the persistent volume rather than via the snapshot image itself. The snapshot image alone is therefore not a self-contained backup of agent state. If you need a portable filesystem snapshot of the host, also copy the contents of `host_dir` separately (e.g. with `mngr pull`).

See [mngr snapshot](../commands/secondary/snapshot.md) for details.

## Tags are immutable

Docker stores tags as container labels, which Docker does not let you mutate after creation. Set tags at create time:

```bash
mngr create my-agent --provider docker --host-label env=test --host-label team=infra
```

`mngr` will refuse `set_host_tags` / `add_tags_to_host` / `remove_tags_from_host` after the container exists. If you need to change a tag, recreate the host (or restore from a snapshot with new labels).

## Remote Docker daemons

Point the provider at a remote daemon by setting `host` in the config or by exporting `DOCKER_HOST`:

```toml
[providers.docker]
backend = "docker"
host = "ssh://user@server"      # or "tcp://host:2376"
```

When `host` is unset, mngr resolves the daemon from `DOCKER_HOST`, then the active Docker context, then the platform default -- the same order the Docker CLI uses.

For remote daemons, the SSH endpoint mngr uses to reach each container is the daemon's hostname (parsed out of `ssh://user@server` or `tcp://host:2376`); for local daemons, it is `127.0.0.1`. The SSH port for each container is auto-assigned by Docker via `-p :22`.

The SSH hostname is derived only from the explicit `host` config field, not from `DOCKER_HOST` or the Docker context. If you point mngr at a remote daemon via `DOCKER_HOST`/context but leave `host` empty, the daemon connection will work but mngr will try to SSH to `127.0.0.1` -- which will fail. Set `host = "ssh://..."` (or `"tcp://..."`) in the provider config when the daemon is not local.

## What else is possible

The Docker provider passes `-b` flags through to `docker build` and `-s` flags through to `docker run`, so anything those CLIs support is available -- secrets via `--build-arg` / `--secret`, multi-stage builds, custom networks, devices via `--device`, ulimits, capabilities, etc. See `docker build --help` and `docker run --help` for the full set, and the [Docker provider reference](../core_plugins/providers/docker.md) for mngr-specific configuration.
