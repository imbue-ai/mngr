# Snapshots

Snapshots capture the complete filesystem state of a [host](./hosts.md). They enable:

- **Stop/start**: State is saved when stopping, restored when starting
- **Backups**: Create manual checkpoints via `mng snapshot`
- **Forking**: Create a new agent from an existing one via `mng clone` (snapshots the source first for remote agents)

## Creating Snapshots

`mng` creates snapshots automatically when stopping an agent. You can also create them manually:

```bash
mng snapshot create my-agent
mng snapshot create my-agent --name "before-refactor"
```

## Using Snapshots

Snapshots are restored automatically when starting a stopped agent. You can also:

```bash
mng create --from-agent my-agent --snapshot <id>   # New agent from snapshot [future]
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
mng snapshot list my-agent
mng snapshot destroy my-agent --snapshot <id> --force
```

See [`mng snapshot`](../commands/secondary/snapshot.md) for all options.
