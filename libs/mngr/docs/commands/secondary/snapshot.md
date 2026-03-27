<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr snapshot

**Synopsis:**

```text
mngr [snapshot|snap] [create|list|destroy] [AGENTS...|-] [OPTIONS]
```

Create, list, and destroy host snapshots.

Snapshots capture the complete filesystem state of a host, allowing it to be
restored later. Because the snapshot is at the host level, the state of all
agents on the host is saved.

Positional arguments to 'create' can be agent names/IDs or host names/IDs.
Each identifier is automatically resolved: if it matches a known agent, that
agent's host is used; otherwise it is treated as a host identifier.

When no subcommand is given, defaults to 'create'. For example,
``mngr snapshot my-agent`` is equivalent to ``mngr snapshot create my-agent``.

Useful for checkpointing work, creating restore points, or managing disk space.

Alias: snap

**Usage:**

```text
mngr snapshot [OPTIONS] COMMAND [ARGS]...
```
**Options:**

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--safe` | boolean | Always query all providers during discovery (disable event-stream optimization). Use this when interfacing with mngr from multiple machines. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## mngr snapshot create

Create a snapshot of agent host(s).

Positional arguments can be agent names/IDs or host names/IDs. Each
identifier is automatically resolved: if it matches a known agent, that
agent's host is snapshotted; otherwise it is treated as a host identifier.
Multiple identifiers that resolve to the same host are deduplicated.

Supports custom format templates via --format. Available fields:
snapshot_id, host_id, provider, agent_names.

**Usage:**

```text
mngr snapshot create [OPTIONS] [IDENTIFIERS]...
```
**Options:**

## Target Selection

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--agent` | text | Agent name or ID to snapshot (can be specified multiple times) | None |
| `--host` | text | Host ID or name to snapshot directly (can be specified multiple times) | None |
| `-a`, `--all`, `--all-agents` | boolean | Snapshot all running agents | `False` |

## Snapshot Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--name` | text | Custom name for the snapshot | None |
| `--dry-run` | boolean | Show what would be snapshotted without actually creating snapshots | `False` |
| `--include` | text | Filter agents by CEL expression (repeatable) [future] | None |
| `--exclude` | text | Exclude agents matching CEL expression (repeatable) [future] | None |
| `--tag` | text | Metadata tag for the snapshot (KEY=VALUE) [future] | None |
| `--description` | text | Description for the snapshot [future] | None |
| `--restart-if-larger-than` | text | Restart host if snapshot exceeds size (e.g., 5G) [future] | None |
| `--pause-during`, `--no-pause-during` | boolean | Pause agent during snapshot creation [future] | `True` |
| `--wait`, `--no-wait` | boolean | Wait for snapshot to complete [future] | `True` |

## Error Handling

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--on-error` | choice (`abort` &#x7C; `continue`) | What to do when errors occur: abort (stop immediately) or continue (keep going) | `continue` |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--safe` | boolean | Always query all providers during discovery (disable event-stream optimization). Use this when interfacing with mngr from multiple machines. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |


## Examples

**Snapshot an agent's host**

```bash
$ mngr snapshot create my-agent
```

**Create a named snapshot**

```bash
$ mngr snapshot create my-agent --name before-refactor
```

**Snapshot all running agents (dry run)**

```bash
$ mngr snapshot create --all --dry-run
```

**Snapshot multiple agents**

```bash
$ mngr snapshot create agent1 agent2 --on-error continue
```

**Custom format template output**

```bash
$ mngr snapshot create my-agent --format '{snapshot_id}'
```

## mngr snapshot list

List snapshots for agent host(s).

Shows snapshot ID, name, creation time, size, and host for each snapshot.

Positional arguments can be agent names/IDs or host names/IDs. Each
identifier is automatically resolved: if it matches a known agent, that
agent's host is used; otherwise it is treated as a host identifier.

Supports custom format templates via --format. Available fields:
id, name, created_at, size, size_bytes, host_id.

**Usage:**

```text
mngr snapshot list [OPTIONS] [IDENTIFIERS]...
```
**Options:**

## Target Selection

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--agent` | text | Agent name or ID to list snapshots for (can be specified multiple times) | None |
| `--host` | text | Host ID or name to list snapshots for directly (can be specified multiple times) | None |
| `-a`, `--all`, `--all-agents` | boolean | List snapshots for all running agents | `False` |

## Filtering

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--limit` | integer | Maximum number of snapshots to show | None |
| `--include` | text | Filter snapshots by CEL expression (repeatable) [future] | None |
| `--exclude` | text | Exclude snapshots matching CEL expression (repeatable) [future] | None |
| `--after` | text | Show only snapshots created after this date [future] | None |
| `--before` | text | Show only snapshots created before this date [future] | None |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--safe` | boolean | Always query all providers during discovery (disable event-stream optimization). Use this when interfacing with mngr from multiple machines. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |


## Examples

**List snapshots for an agent**

```bash
$ mngr snapshot list my-agent
```

**List snapshots for all running agents**

```bash
$ mngr snapshot list --all
```

**Limit number of results**

```bash
$ mngr snapshot list my-agent --limit 5
```

**Output as JSON**

```bash
$ mngr snapshot list my-agent --format json
```

**Custom format template**

```bash
$ mngr snapshot list my-agent --format '{name}\t{size}\t{host_id}'
```

## mngr snapshot destroy

Destroy snapshots for agent host(s).

Requires either --snapshot (to delete specific snapshots) or --all-snapshots
(to delete all snapshots for the resolved hosts). A confirmation prompt is
shown unless --force is specified.

Supports custom format templates via --format. Available fields:
snapshot_id, host_id, provider.

**Usage:**

```text
mngr snapshot destroy [OPTIONS] [AGENTS]...
```
**Options:**

## Target Selection

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--agent` | text | Agent name or ID whose snapshots to destroy (can be specified multiple times) | None |
| `--snapshot` | text | Snapshot ID to destroy (can be specified multiple times) | None |
| `--all-snapshots` | boolean | Destroy all snapshots for the specified agent(s) | `False` |

## Safety

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `-f`, `--force` | boolean | Skip confirmation prompt | `False` |
| `--dry-run` | boolean | Show what would be destroyed without actually deleting | `False` |
| `--include` | text | Filter snapshots by CEL expression (repeatable) [future] | None |
| `--exclude` | text | Exclude snapshots matching CEL expression (repeatable) [future] | None |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--safe` | boolean | Always query all providers during discovery (disable event-stream optimization). Use this when interfacing with mngr from multiple machines. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |


## Examples

**Destroy a specific snapshot**

```bash
$ mngr snapshot destroy my-agent --snapshot snap-abc123 --force
```

**Destroy all snapshots for an agent**

```bash
$ mngr snapshot destroy my-agent --all-snapshots --force
```

**Preview what would be destroyed**

```bash
$ mngr snapshot destroy my-agent --all-snapshots --dry-run
```

## See Also

- [mngr create](../primary/create.md) - Create a new agent (supports --snapshot to restore from snapshot)
- [mngr gc](./gc.md) - Garbage collect unused resources including snapshots

## Examples

**Snapshot an agent's host (short form)**

```bash
$ mngr snapshot my-agent
```

**Snapshot an agent's host (explicit)**

```bash
$ mngr snapshot create my-agent
```

**Create a named snapshot**

```bash
$ mngr snapshot create my-agent --name before-refactor
```

**Snapshot by host ID**

```bash
$ mngr snapshot create my-host-id
```

**Snapshot all running agents**

```bash
$ mngr snapshot create --all --dry-run
```

**List snapshots for an agent**

```bash
$ mngr snapshot list my-agent
```

**Destroy all snapshots for an agent**

```bash
$ mngr snapshot destroy my-agent --all-snapshots --force
```

**Preview what would be destroyed**

```bash
$ mngr snapshot destroy my-agent --all-snapshots --dry-run
```
