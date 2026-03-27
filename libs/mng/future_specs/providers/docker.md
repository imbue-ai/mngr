# Docker Provider Spec

## Metadata Storage

Host metadata is stored as Docker container labels. When a host is created, mng sets labels on the container:

```
com.imbue.mng.host-id=<host-id>
com.imbue.mng.host-name=<host-name>
com.imbue.mng.provider=<provider-instance-name>
com.imbue.mng.tags=<json-encoded-tags>
```

Labels are preserved across container stop/start cycles and are included in committed images (for snapshots).

Docker labels cannot be changed after being set. If a user attempts to mutate them, mng will raise an error.

## Host Discovery

mng discovers Docker hosts by listing containers with the `com.imbue.mng.host-id` label.

## Agent Self-Management

Unlike Modal sandboxes, Docker containers have a simpler mechanism for self-stopping. An agent running inside a Docker container can stop the container by killing the process with PID 1 (the container's init process).

When PID 1 terminates, Docker automatically stops the container. This provides a straightforward way for agents to stop themselves without requiring external API access.

## Snapshots

Snapshots are created via `docker commit`. The resulting image is tagged with:

```
mng-snapshot:<host-id>-<snapshot-name>
```

### Snapshot Constraints

Certain Docker configurations are incompatible with snapshotting or make snapshots unreliable:

- **Bind mounts**: Snapshots only capture the container's filesystem layers, not bind-mounted host directories. If critical state is stored in bind mounts, it will be lost.
- **GPU access**: Containers using GPU resources may have hardware-specific state that cannot be captured in snapshots.
- **Shared volumes**: Like bind mounts, volumes shared between containers are not included in snapshots.
- **Network-attached storage**: Any external storage mounted into the container will not be captured.

When volume mounts are detected, mng logs a warning but proceeds with the snapshot. Detection of GPU and network-attached storage constraints is not yet implemented.
