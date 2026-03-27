# Provider Instances

A **provider instance** creates and manages [hosts](./hosts.md). Each provider instance is a configured endpoint of a [provider backend](./provider_backends.md).

From the perspective of `[pyinfra](https://pyinfra.com/)` (the tool we suggest for [provisioning](./provisioning.md)), you can think of provider instances as "something that mutates the inventory" (eg, create, destroy, stop, start, etc.)

A default provider instance is automatically created for each registered backend (e.g., `local`, `docker`), but you can also define your own in your `mng` settings:

```toml
[providers.my-aws-prod]
backend = "aws"
region = "us-east-1"
profile = "production"

[providers.my-aws-dev]
backend = "aws"
region = "us-west-2"
profile = "development"

[providers.remote-docker]
backend = "docker"
host = "ssh://user@server"

[providers.team-mng]
backend = "mng"
url = "https://mng.internal.company.com"
```

## Built-in Provider Instances

### local

Runs agents directly on your machine with no isolation. Always available--no configuration required.

### docker

Runs agents in Docker containers. Available as long as `docker` is installed.

Provides container isolation while keeping everything local or on a remote Docker daemon. Uses SSH for host operations after initial container setup.

## Responsibilities

Provider instances must handle:

- **Create** — Build images, allocate resources, start the host
- **Stop** — Stop the host, optionally create a snapshot
- **Start** — Restore from snapshot, restart the host
- **Destroy** — Clean up all resources associated with the host
- **Snapshot** — Capture filesystem state for backup/restore (optional, not supported by all providers)
- **List** — Discover all mng-managed hosts
- **CLI args** — Register provider-specific flags (e.g., `--gpu`, `--memory`)

`mng` handles higher-level concerns: agent lifecycle, idle detection, port forwarding, and file sync.

See [`provider_instance.py`](../../imbue/mng/interfaces/provider_instance.py) and [`provider_backend.py`](../../imbue/mng/interfaces/provider_backend.py) for the full API that provider implementations must support.

## State Storage

Each provider instance should store *all* of its state *in the provider itself*. 
This helps ensure that such state could be accessed by other (remote) `mng` instances if needed.
It also helps to keep `mng` stateless (`mng` should reconstruct the necessary state for any given command by querying provider instances, which then load the remote state).

This state storage is typically accomplished via tags, remote disks/volumes, and other provider-specific metadata storage.

By convention, the state stored in the host data.json should be contained in the state for the provider as well (since it allows for offline access to the certified data)
