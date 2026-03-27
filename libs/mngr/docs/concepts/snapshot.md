# Snapshots

Snapshots capture the complete filesystem state of a [host](./hosts.md). They enable:

- **Stop/start**: State is saved when stopping, restored when starting
- **Backups**: Create manual checkpoints via `mngr snapshot`
- **Forking**: Create a new agent from an existing one via `mngr clone` (snapshots the source first for remote agents)

## Creating Snapshots

`mngr` creates snapshots automatically when stopping an agent. You can also create them manually:

```bash
mngr snapshot create my-agent
mngr snapshot create my-agent --name "before-refactor"
```

## Using Snapshots

Snapshots are restored automatically when starting a stopped agent. You can also:

```bash
mngr create --from-agent my-agent --snapshot <id>   # New agent from snapshot [future]
```

## Consistency

Snapshot semantics are "hard power off": in-flight writes may not be captured. For databases or other stateful applications, this is usually fine since they're designed to survive power loss.

By default, hosts are paused during snapshotting to improve consistency. This can be disabled via the `--no-pause-during` flag [future], but doing so may lead to corrupted files in the snapshot.

## Provider Support

Snapshot support varies by [provider](./providers.md):

- **Local**: Not supported
- **Docker**: `docker commit` (incremental relative to container start, can be slow for large containers)
- **Modal**: Native snapshots (fast, fully incremental since the last snapshot)

## Managing Snapshots

List and clean up snapshots:

```bash
mngr snapshot list my-agent
mngr snapshot destroy my-agent --snapshot <id> --force
```

See [`mngr snapshot`](../commands/secondary/snapshot.md) for all options.
