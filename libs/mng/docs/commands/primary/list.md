<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng list

**Synopsis:**

```text
mng [list|ls] [OPTIONS]
```

List all agents managed by mng.

Displays agents with their status, host information, and other metadata.
Supports filtering, sorting, and multiple output formats.

Alias: ls

**Usage:**

```text
mng list [OPTIONS]
```
**Options:**

## Filtering

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--include` | text | Include agents matching CEL expression (repeatable) | None |
| `--exclude` | text | Exclude agents matching CEL expression (repeatable) | None |
| `--running` | boolean | Show only running agents (alias for --include 'state == "RUNNING"') | `False` |
| `--stopped` | boolean | Show only stopped agents (alias for --include 'state == "STOPPED"') | `False` |
| `--local` | boolean | Show only local agents (alias for --include 'host.provider == "local"') | `False` |
| `--remote` | boolean | Show only remote agents (alias for --exclude 'host.provider == "local"') | `False` |
| `--provider` | text | Show only agents using specified provider (repeatable) | None |
| `--project` | text | Show only agents with this project label (repeatable) | None |
| `--label` | text | Show only agents with this label (format: KEY=VALUE, repeatable) [experimental] | None |
| `--tag` | text | Show only agents on hosts with this tag (format: KEY=VALUE, repeatable) | None |
| `--stdin` | boolean | Read agent and host IDs or names from stdin (one per line) | `False` |

## Output Format

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--fields` | text | Which fields to include (comma-separated) | None |
| `--sort` | text | Sort by CEL expression(s) with optional direction, e.g. 'name asc, create_time desc'; enables sorted (non-streaming) output [default: create_time] | `create_time` |
| `--limit` | integer | Limit number of results (applied after fetching from all providers) | None |

## Watch / Stream Mode

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `-w`, `--watch` | integer | Continuously watch and update status at specified interval (seconds) | None |
| `--stream` | boolean | Stream discovery events as JSONL. Outputs a full snapshot, then tails the event file for updates. Periodically re-polls to catch any missed changes. | `False` |

## Error Handling

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--on-error` | choice (`abort` &#x7C; `continue`) | What to do when errors occur: abort (stop immediately) or continue (keep going) | `abort` |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mng/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNG_HEADLESS env var or 'headless' config key. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## CEL Filter Examples

CEL (Common Expression Language) filters allow powerful, expressive filtering of agents.
All agent fields from the "Available Fields" section can be used in filter expressions.

**Simple equality filters:**
- `name == "my-agent"` - Match agent by exact name
- `state == "RUNNING"` - Match running agents
- `host.provider == "docker"` - Match agents on Docker hosts
- `type == "claude"` - Match agents of type "claude"
- `labels.project == "mng"` - Match agents with a specific project label

**Compound expressions:**
- `state == "RUNNING" && host.provider == "modal"` - Running agents on Modal
- `state == "STOPPED" || state == "FAILED"` - Stopped or failed agents
- `host.provider == "docker" && name.startsWith("test-")` - Docker agents with names starting with "test-"

**String operations:**
- `name.contains("prod")` - Agent names containing "prod"
- `name.startsWith("staging-")` - Agent names starting with "staging-"
- `name.endsWith("-dev")` - Agent names ending with "-dev"

**Numeric comparisons:**
- `runtime_seconds > 3600` - Agents running for more than an hour
- `idle_seconds < 300` - Agents active in the last 5 minutes
- `host.resource.memory_gb >= 8` - Agents on hosts with 8GB+ memory
- `host.uptime_seconds > 86400` - Agents on hosts running for more than a day

**Existence checks:**
- `has(url)` - Agents that have a URL set
- `has(host.ssh)` - Agents on remote hosts with SSH access



## Available Fields

**Agent fields** (same syntax for `--fields` and CEL filters):
- `name` - Agent name
- `id` - Agent ID
- `type` - Agent type (claude, codex, etc.)
- `command` - The command used to start the agent
- `url` - URL where the agent can be accessed (if reported)
- `work_dir` - Working directory for this agent
- `initial_branch` - Git branch name created for this agent
- `create_time` - Creation timestamp
- `start_time` - Timestamp for when the agent was last started
- `runtime_seconds` - How long the agent has been running
- `user_activity_time` - Timestamp of the last user activity
- `agent_activity_time` - Timestamp of the last agent activity
- `idle_seconds` - How long since the agent was active
- `idle_mode` - Idle detection mode
- `idle_timeout_seconds` - Idle timeout before host stops
- `activity_sources` - Activity sources used for idle detection
- `start_on_boot` - Whether the agent is set to start on host boot
- `state` - Agent lifecycle state (RUNNING, STOPPED, WAITING, REPLACED, DONE)
- `labels` - Agent labels (key-value pairs, e.g., project=mng)
- `labels.$KEY` - Specific label value (e.g., `labels.project`)
- `plugin.$PLUGIN_NAME.*` - Plugin-defined fields (e.g., `plugin.chat_history.messages`)

**Host fields** (dot notation for both `--fields` and CEL filters):
- `host.name` - Host name
- `host.id` - Host ID
- `host.provider_name` - Host provider (local, docker, modal, etc.) (in CEL filters, use `host.provider`)
- `host.state` - Current host state (RUNNING, STOPPED, BUILDING, etc.)
- `host.image` - Host image (Docker image name, Modal image ID, etc.)
- `host.tags` - Metadata tags for the host
- `host.ssh_activity_time` - Timestamp of the last SSH connection to the host
- `host.boot_time` - When the host was last started
- `host.uptime_seconds` - How long the host has been running
- `host.resource` - Resource limits for the host
  - `host.resource.cpu.count` - Number of CPUs
  - `host.resource.cpu.frequency_ghz` - CPU frequency in GHz
  - `host.resource.memory_gb` - Memory in GB
  - `host.resource.disk_gb` - Disk space in GB
  - `host.resource.gpu.count` - Number of GPUs
  - `host.resource.gpu.model` - GPU model name
  - `host.resource.gpu.memory_gb` - GPU memory in GB
- `host.ssh` - SSH access details (remote hosts only)
  - `host.ssh.command` - Full SSH command to connect
  - `host.ssh.host` - SSH hostname
  - `host.ssh.port` - SSH port
  - `host.ssh.user` - SSH username
  - `host.ssh.key_path` - Path to SSH private key
- `host.snapshots` - List of available snapshots
- `host.is_locked` - Whether the host is currently locked for an operation
- `host.locked_time` - When the host was locked
- `host.plugin.$PLUGIN_NAME.*` - Host plugin fields (e.g., `host.plugin.aws.iam_user`)

**Notes:**
- You can use Python-style list slicing for list fields (e.g., `host.snapshots[0]` for the first snapshot, `host.snapshots[:3]` for the first 3)



## Related Documentation

- [Multi-target Options](../generic/multi_target.md) - Behavior when some agents cannot be accessed
- [Common Options](../generic/common.md) - Common CLI options for output format, logging, etc.

## See Also

- [mng create](./create.md) - Create a new agent
- [mng connect](./connect.md) - Connect to an existing agent
- [mng destroy](./destroy.md) - Destroy agents

## Examples

**List all agents**

```bash
$ mng list
```

**List only running agents**

```bash
$ mng list --running
```

**List agents on Docker hosts**

```bash
$ mng list --provider docker
```

**List agents for a project**

```bash
$ mng list --project mng
```

**List agents with a specific label**

```bash
$ mng list --label env=prod
```

**List agents with a specific host tag**

```bash
$ mng list --tag env=prod
```

**List agents as JSON**

```bash
$ mng list --format json
```

**Filter with CEL expression**

```bash
$ mng list --include 'name.contains("prod")'
```

**Sort by name descending**

```bash
$ mng list --sort 'name desc'
```

**Sort by multiple fields**

```bash
$ mng list --sort 'state, name asc, create_time desc'
```
