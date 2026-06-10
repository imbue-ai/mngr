# Using Docker

Run coding agents in Docker containers. For general agent management, see [mngr create](../commands/primary/create.md). For the full list of Docker provider arguments, see the [Docker provider reference](../core_plugins/providers/docker.md).

## Prerequisites

- Docker installed and a reachable daemon (local Docker Desktop, a remote daemon via `DOCKER_HOST`, or a configured Docker context)
- mngr installed and working locally

The Docker client used by mngr resolves the daemon in the same order as the Docker CLI: `DOCKER_HOST`, then the active Docker context (from `~/.docker/config.json`), then the platform default.

## Creating a local agent

From any git repo:

```bash
mngr create my-agent --provider docker
```

This builds a container, drops you into a tmux session, and gives you the same interactive experience as a local agent. Equivalently you can use the address form `mngr create my-agent@.docker`.

If you do not pass an image or a Dockerfile, mngr builds a default image from `debian:bookworm-slim` with the packages it needs (`openssh-server`, `tmux`, `curl`, `rsync`, `git`, `jq`, `xxd`, `ca-certificates`). For faster startup on repeated creates, supply your own image (see below) so these packages are pre-installed alongside your project's dependencies.

### How `-b` and `-s` flags work

The Docker provider passes `-b` (or `--build-arg`) flags straight through to `docker build` and `-s` (or `--start-arg`) flags straight through to `docker run`. So anything those CLIs support is available -- `-b --no-cache`, `-b --build-arg=KEY=VAL`, `-s --device=...`, `-s --ulimit=...`, capabilities, secrets, networks, etc. Check `docker build --help` and `docker run --help` for the full set.

### Using a template

If your project has a Docker template defined in `.mngr/settings.toml`, you can use `-t my-docker` instead of passing flags manually:

```bash
mngr create my-agent -t my-docker
```

A typical Docker template builds the project's own Dockerfile and points the agent at the path inside the container where the source ends up:

```toml
[create_templates.my-docker]
provider = "docker"
build_arg = ["--file=path/to/Dockerfile", "build/context/dir"]
target_path = "/code/my-project"
agent_args = ["--dangerously-skip-permissions"]
pass_env = ["GH_TOKEN"]
pass_host_env = ["EDITOR"]
```

`build_arg` entries are appended to `docker build -t <generated-tag>` (so the last entry is the build context). `pass_host_env = ["EDITOR"]` forwards your local `$EDITOR` into the container so commands like `git commit` open the editor you actually use rather than whatever fallback the base image happens to provide. The container is an isolated environment, so `--dangerously-skip-permissions` is reasonable for the container itself -- but credentials forwarded via `pass_env` (e.g. `GH_TOKEN`) can still be used by the agent without confirmation. The container can also read/write any bind-mounted host paths you pass via `-s -v=...`, so do not rely on the container as a strong sandbox if you mount sensitive host directories.

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

Each host's `host_dir` (e.g. `/mngr`) is backed by a sub-folder of a shared Docker named volume (`<prefix>docker-state-<user_id>`). The state container always mounts the full volume at `/mngr-state` so mngr can read host metadata and per-host data even when the host container is stopped. How that storage is exposed *inside the host container* depends on the `isolate_host_volumes` provider config field:

- **`isolate_host_volumes = true` (recommended; default in a future release).** Each host container sees only its own sub-folder, mounted directly at `host_dir` via `--mount type=volume,source=<vol>,target=<host_dir>,volume-subpath=volumes/vol-<host_hex>`. Sibling hosts are invisible. Requires Docker Engine >= 25.0 (the version that introduced `volume-subpath`); mngr fails fast at `mngr create` if the daemon is older.
- **`isolate_host_volumes = false`.** Each host container mounts the entire shared volume at `/mngr-state` and `host_dir` is symlinked to `/mngr-state/volumes/vol-<host_hex>/`. This is today's behavior; it means an agent in one host can read every other host's `host_dir` under the same `user_id`. Pick this only if you actively want that cross-host visibility.
- **`isolate_host_volumes` unset.** Same on-the-wire behavior as `false`, but mngr emits a one-shot warning at startup that the default will flip to `true` in a future release. Set the field explicitly (either way) to silence the warning.

The choice is *sticky per host*: each newly-created host records its mount strategy into its `HostRecord`, and every later start / restart / snapshot-restore replays the same strategy regardless of any later config change. Switching the provider config only affects hosts created after the switch.

Either mode keeps the state container's offline-access story intact: `mngr events`, `mngr list`, and `mngr volume`-style reads all go through the state container, which always mounts the full volume.

You can also disable persistence entirely by setting `is_host_volume_created = false`. The `host_dir` then lives on the container's overlay filesystem; it survives stop/start (Docker preserves the container filesystem) but is not accessible while the container is stopped. The combination `is_host_volume_created = false, isolate_host_volumes = true` is rejected at config load time.

User-supplied bind mounts (`-s -v=...`) are independent of the host volume. They are **not** captured in snapshots -- only the container's filesystem layers are.

## Getting changes back

### Option A: Give the agent git credentials

If the agent has `GH_TOKEN` (via `pass_env` in a template or `--pass-env` on the CLI), it can `git push` directly.

### Option B: Use `mngr rsync` or `mngr git pull`

`mngr rsync` and `mngr git pull` transfer changes from the agent to your local
machine without needing git credentials on the agent.

**Pull git commits** (when the agent has committed its work):

```bash
mngr git pull my-agent
```

This merges the agent's branch into your current local branch.

**Pull files** (works for uncommitted changes and non-git-tracked files):

```bash
mngr rsync my-agent ./
```

This uses rsync over SSH to sync the agent's working directory to your current
directory. To preview what would be transferred first:

```bash
mngr rsync my-agent ./ --dry-run
```

You can also pull a specific subdirectory:

```bash
mngr rsync my-agent:src ./local-src
```

To push local changes to the agent (e.g. a config file you edited locally):

```bash
mngr rsync ./config my-agent:config
```

See [mngr rsync](../commands/primary/rsync.md) and [mngr git](../commands/primary/git.md) for all options.

### Option C: Fetch the branch directly from the host volume

When `is_host_volume_created = true` (the default) and the agent's work directory lives under `host_dir` (the default for worktree/git-mirror transfer modes -- e.g. `/mngr/worktrees/<name>-<uuid>/`), the agent's git repo sits on the shared Docker named volume. You can git-fetch from it without involving SSH or `mngr git pull`.

On **Linux**, the volume is a real path on the daemon host, so you can fetch from it directly:

```bash
# Find the agent's work dir on the volume (one entry per agent):
sudo ls /var/lib/docker/volumes/<prefix>docker-state-<user_id>/_data/volumes/vol-<host_hex>/worktrees/

# Add it as a remote and fetch the agent's branch into your local checkout:
git remote add my-agent /var/lib/docker/volumes/<prefix>docker-state-<user_id>/_data/volumes/vol-<host_hex>/worktrees/<name>-<uuid>
git fetch my-agent
git merge my-agent/<branch-name>     # or: git checkout my-agent/<branch-name>
```

On **macOS Docker Desktop or remote daemons**, the volume isn't mounted on your filesystem. Copy the agent's `.git` (or the whole work dir) out via the state container, then fetch from the copy:

```bash
# Copy the agent's work dir out of the volume:
docker cp <prefix>docker-state-<user_id>:/mngr-state/volumes/vol-<host_hex>/worktrees/<name>-<uuid> /tmp/my-agent

# Fetch the branch from the copy:
git remote add my-agent /tmp/my-agent
git fetch my-agent
git merge my-agent/<branch-name>
```

This bypasses SSH entirely and is the fastest option when the agent has already committed. It only works when the work dir is under `host_dir` -- if you set `target_path` to a path outside `host_dir` (e.g. `/code/my-project`), the work dir is on the container's overlay filesystem instead, and you need Option A or B.

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

Snapshots are stored as Docker images (`mngr-snapshot:<host_id>-<name>`). They capture the container's filesystem layers but **not** the contents of any volumes -- bind mounts (`-s -v=...`), named volumes, or the shared host volume. When mngr restores a host from a snapshot, it re-mounts the same host volume sub-folder, so anything the agent wrote under `host_dir` (e.g. `/mngr`) reappears via the persistent volume rather than via the snapshot image itself. The snapshot image alone is therefore not a self-contained backup of agent state. If you need a portable filesystem snapshot of the host, also copy the contents of `host_dir` separately (e.g. with `mngr rsync`).

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

See the [Docker provider reference](../core_plugins/providers/docker.md) for the full list of provider config options. Anything supported by `docker build` / `docker run` is reachable via `-b` / `-s` -- secrets via `--secret`, multi-stage builds, `--device`, `--cap-add`, `--ulimit`, custom networks, etc.
