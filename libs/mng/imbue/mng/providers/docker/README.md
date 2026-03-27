# Docker Provider -- Architecture

This document describes the internal architecture of the Docker provider.
For user-facing documentation, see `docs/core_plugins/providers/docker.md`.

## Overview

The Docker provider manages Docker containers as mng hosts. 
Each container runs sshd and is accessed via pyinfra's SSH connector, following the same pattern as the Modal provider. 
The key difference is that Docker supports native stop/start (containers are stopped, not destroyed) and snapshots are implemented via `docker commit`.

## State Container and State Volume

All provider-level metadata (host records, agent data, per-host volumes) is stored on a Docker named volume.

This is done so that multiple remote mng clients can connect to a shared Docker daemon and see the same hosts, agents, and data (if they have the same user_id), and different users on the same Docker daemon are isolated via different volume namespaces (based on user_id).

This volume is mounted into a singleton "state container" -- a small Alpine container that stays running and acts as a file server. 
All file operations against the state volume are performed by exec-ing commands (`cat`, `ls`, `mkdir`, `rm`) in this container, or by using `put_archive` for writes.

```
Docker Named Volume: <prefix>docker-state-<user_id>
    mounted at /mng-state inside the state container

State Container: <prefix>docker-state-<user_id>
    image: alpine:latest
    restart: unless-stopped
    purpose: provides exec target for all volume I/O
```

The state container is created lazily by `ensure_state_container()` in `volume.py` the first time the provider instance accesses `_state_volume`.

### State Volume Directory Layout

```
/mng-state/
    host_state/
        <host_id>.json              # HostRecord (SSH info, config, certified data)
        <host_id>/
            <agent_id>.json         # Persisted agent data (for offline listing)
    volumes/
        <host_id>/                  # Per-host volume directory
            .volume                 # Marker file (created during host creation)
            agents/
                <agent_id>/         # Per-agent scoped data
                    ...
```

#### host_state/

The `host_state/` directory contains `HostRecord` JSON files. Each record stores everything needed to reconnect to a host:

- `certified_host_data`: the canonical host metadata (name, tags, snapshots, failure reason, timestamps, idle config)
- `ssh_host`, `ssh_port`, `ssh_host_public_key`: SSH connection info
- `config`: `ContainerConfig` (start_args, image) for replay on snapshot restore
- `container_id`: Docker container ID

For failed hosts (creation failure), only `certified_host_data` is populated; the SSH fields and config are `None`.

Agent data is persisted alongside host records at `host_state/<host_id>/<agent_id>.json` so agents can be listed even when the host is offline.

#### volumes/ (per-host persistent storage)

When `is_host_volume_created` is True (the default), each host gets a dedicated sub-folder at `volumes/<host_id>/` on the state volume. 
This directory is **bind-mounted into the host container** by mounting the entire Docker named volume into the container and then symlinking the `host_dir` (e.g., `/mng`) to `<mount_path>/volumes/<host_id>/`.

The symlink setup is handled by `build_check_and_install_packages_command` (from `ssh_host_setup.py`), the same mechanism used by the Modal provider.

This gives us:
- **Persistent host data**: all files written to `host_dir` by agents are stored on the Docker named volume, not in the container's overlay filesystem.
- **Offline access**: data is readable via the state container (and `get_volume_for_host()`) even when the host container is stopped.
- **Shared volume**: both the state container and host containers mount the same Docker named volume, so mng can read host-written data directly.

When `is_host_volume_created` is False, `host_dir` is a regular directory inside the container (created via `mkdir -p`), and `get_volume_for_host()` returns None. 
Data is still preserved across stop/start (Docker preserves the container filesystem), but is not accessible while the container is stopped.

When a host is destroyed via `destroy_host()`, the volume directory is cleaned up.

## SSH Architecture

Each Docker container runs sshd for pyinfra access. The SSH setup uses:

1. **Client keypair** (`docker_ssh_key` / `docker_ssh_key.pub`): stored in the profile directory at `~/.mng/<profile>/providers/docker/<instance>/keys/`. One keypair is shared across all containers for a given provider instance.

2. **Host keypair** (`host_key` / `host_key.pub`): also stored in the profile directory. Injected into each container so we can pre-trust the host key and avoid host key verification prompts.

3. **known_hosts**: maintained at the same keys directory. Updated each time a container is created or reconnected.

SSH setup is performed via `docker exec`. The shared helpers in `providers/ssh_host_setup.py` generate shell commands that:
- Install openssh-server, tmux, python3, rsync if missing
- Configure the SSH authorized_keys and host key
- Start sshd in the background

## Container Lifecycle

### Creation

```
create_host(name, image, ...)
    1. Pull base image (or build from Dockerfile)
    2. Create host volume directory at volumes/<host_id>/ (if enabled)
    3. Run container: docker run -d --name <prefix><name> -p :22
       -v <state_volume>:/mng-state:rw ...
    4. Install packages via docker exec; symlink host_dir ->
       /mng-state/volumes/<host_id>/ (if volume enabled, else mkdir)
    5. Configure SSH via docker exec
    6. Start sshd via docker exec (detached)
    7. Wait for sshd to accept connections
    8. Create pyinfra Host object
    9. Write HostRecord to state volume
    10. Create shutdown.sh script on the host
    11. Start activity watcher
```

### Stop

```
stop_host(host, create_snapshot=True)
    1. Optionally create snapshot (docker commit)
    2. docker stop (SIGTERM to PID 1, which traps and exits cleanly)
    3. Update host record with stop_reason
```

### Start (native restart)

```
start_host(host_id)
    1. docker start (restarts stopped container, filesystem preserved)
    2. Re-run SSH setup (sshd, keys, etc.)
    3. Return new Host object
```

### Start (from snapshot)

```
start_host(host_id, snapshot_id)
    1. Remove old container
    2. docker run from committed image (snapshot)
    3. Re-run SSH setup
    4. Return new Host object
```

### Destroy

```
destroy_host(host, delete_snapshots=True)
    1. Stop container (no snapshot)
    2. docker rm -f
    3. Delete snapshot images
    4. Delete host record from state volume
    5. Delete host volume directory
```

## Container Entrypoint

All containers (both host containers and the state container) use the same entrypoint:

```sh
trap 'exit 0' TERM; tail -f /dev/null & wait
```

This keeps PID 1 alive (via `tail -f /dev/null`) and responds to SIGTERM with a clean exit (exit code 0). This is important because `docker stop` sends SIGTERM, and we want containers to exit cleanly.

## Container Labels

Docker containers are labeled with mng metadata for discovery:

- `com.imbue.mng.host-id`: the HostId
- `com.imbue.mng.host-name`: the HostName
- `com.imbue.mng.provider`: the provider instance name
- `com.imbue.mng.tags`: JSON-encoded user tags

These labels are used by `_find_container_by_host_id()` and `_find_container_by_name()` for fast container lookup via Docker API filters. 
Tags are immutable after creation (Docker does not support label mutation).

## Snapshots

Snapshots use `docker commit` to create a new image from a running container. The committed image ID is stored in the host record's `certified_host_data.snapshots` list. 
Restoring from a snapshot creates a new container from the committed image (the old container is removed).

Note: Docker volume mounts are NOT captured in snapshots. Only the container's filesystem layers are committed.
For this reason, users shoudl call `mng snapshot` instead of using `docker commit` directly, since `mng` is also able to make a copy of the host volume directory properly. 
