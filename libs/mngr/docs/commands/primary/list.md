<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr list

**Synopsis:**

```text
mngr [list|ls] [OPTIONS]
```

List all agents managed by mngr.

Displays agents with their status, host information, and other metadata.
Supports filtering, sorting, and multiple output formats.

Alias: ls

**Usage:**

```text
mngr list [OPTIONS]
```
**Options:**

## Filtering

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--include` | text | Include agents matching CEL expression (repeatable) | None |
| `--exclude` | text | Exclude agents matching CEL expression (repeatable) | None |
| `--running` | boolean | Show only running agents (alias for --include 'state == "RUNNING"') | `False` |
| `--stopped` | boolean | Show only stopped agents (alias for --include 'state == "STOPPED"') | `False` |
| `--archived` | boolean | Show only archived agents (alias for --include 'has(labels.archived_at)') | `False` |
| `--active` | boolean | Show only active agents (anything not archived/destroyed/crashed/failed) | `False` |
| `--local` | boolean | Show only local agents (alias for --include 'host.provider == "local"') | `False` |
| `--remote` | boolean | Show only remote agents (alias for --exclude 'host.provider == "local"') | `False` |
| `--project` | text | Show only agents with this project label (repeatable; '.' expands to the current project) | None |
| `--label` | text | Show only agents with this label (format: KEY=VALUE, repeatable) [experimental] | None |
| `--host-label` | text | Show only agents on hosts with this host label (format: KEY=VALUE, repeatable) | None |
| `--provider` | text | Show only agents using specified provider (repeatable) | None |
| `--stdin` | boolean | Read agent and host IDs or names from stdin (one per line) | `False` |

## Output Format

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--ids` | boolean | Print only agent IDs, one per line | `False` |
| `--addrs` | boolean | Print only agent addresses (name@host.provider), one per line | `False` |
| `--schema` | boolean | List the fields referenceable in --include/--exclude, --sort, and --fields/--format (with their types and the contexts they work in), instead of listing agents. | `False` |
| `--fields` | text | Which fields to include (comma-separated) | None |
| `--header` | text | Override column header label (format: FIELD=LABEL, repeatable) | None |
| `--sort` | text | Sort by CEL expression(s) with optional direction, e.g. 'name asc, create_time desc'; enables sorted (non-streaming) output [default: create_time] | `create_time` |
| `--limit` | integer | Limit number of results (applied after fetching from all providers) | None |

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
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--safe` | boolean | Always query all providers during discovery (disable event-stream optimization). Use this when interfacing with mngr from multiple machines. | `False` |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-S`, `--setting` | text | Override a config setting for this invocation (KEY=VALUE, dot-separated paths; append __extend to the leaf key to extend list/dict/set fields) [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## CEL Filter Examples

CEL (Common Expression Language) filters allow powerful, expressive filtering of agents.
All agent fields from the "Available Fields" section can be used in filter expressions.

**Simple equality filters:**
- `name == "my-agent"` - Match agent by exact name
- `state == "RUNNING"` - Match running agents
- `host.provider == "docker"` - Match agents on Docker hosts
- `type == "claude"` - Match agents of type "claude"
- `labels.project == "mngr"` - Match agents with a specific project label

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
- `has(labels.foo)` - Agents that have a `foo` label set



## Available Fields

Every field below can be used in CEL expressions for `--include`/`--exclude` and `--sort` (these share one evaluation context, so `cel` covers both).

Each field is annotated with the contexts it works in:
- `cel` - usable in `--include`/`--exclude` and `--sort`
- `template` - also usable in `--fields` and `--format` template strings

Only the computed fields (age/runtime/idle) are `cel`-only; everything else is both.

**Agent fields:**
- `id` (cel, template) - Agent ID
- `name` (cel, template) - Agent name
- `type` (cel, template) - Agent type (claude, codex, etc.)
- `command` (cel, template) - Command used to start the agent
- `work_dir` (cel, template) - Working directory
- `initial_branch` (cel, template) - Git branch name created for this agent
- `create_time` (cel, template) - Creation timestamp
- `start_on_boot` (cel, template) - Whether agent starts on host boot
- `state` (cel, template) - Agent lifecycle state (STOPPED/RUNNING/WAITING/REPLACED/RUNNING_UNKNOWN_AGENT_TYPE/DONE/UNKNOWN)
- `url` (cel, template) - Agent URL (reported)
- `start_time` (cel, template) - Last start time (reported)
- `runtime_seconds` (cel, template) - Runtime in seconds
- `user_activity_time` (cel, template) - Last user activity (reported)
- `agent_activity_time` (cel, template) - Last agent activity (reported)
- `idle_seconds` (cel, template) - Idle time in seconds
- `idle_mode` (cel, template) - Idle detection mode
- `idle_timeout_seconds` (cel, template) - Idle timeout in seconds
- `activity_sources` (cel, template) - Activity sources used for idle detection
- `labels` (cel, template) - Agent labels (key-value pairs)
- `host` (cel, template) - Host information
- `plugin` (cel, template) - Plugin-specific fields

**Host fields:**
- `host.id` (cel, template) - Host ID
- `host.name` (cel, template) - Host name
- `host.provider_name` (cel, template) - Provider that owns the host
- `host.state` (cel, template) - Current host state (RUNNING, STOPPED, etc.)
- `host.image` (cel, template) - Host image (Docker image name, Modal image ID, etc.)
- `host.tags` (cel, template) - Metadata tags for the host
- `host.boot_time` (cel, template) - When the host was last started
- `host.uptime_seconds` (cel, template) - How long the host has been running
- `host.resource` (cel, template) - Resource limits for the host
- `host.resource.cpu` (cel, template) - CPU resources
- `host.resource.cpu.count` (cel, template) - Number of CPUs allocated to the host
- `host.resource.cpu.frequency_ghz` (cel, template) - CPU frequency in GHz (None if not reported by provider)
- `host.resource.memory_gb` (cel, template) - Allocated memory in GB
- `host.resource.disk_gb` (cel, template) - Allocated disk space in GB (None if not reported)
- `host.resource.gpu` (cel, template) - GPU resources (None if no GPU allocated)
- `host.resource.gpu.count` (cel, template) - Number of GPUs allocated to the host
- `host.resource.gpu.model` (cel, template) - GPU model name (e.g., 'NVIDIA A100')
- `host.resource.gpu.memory_gb` (cel, template) - GPU memory in GB per GPU
- `host.ssh` (cel, template) - SSH access details (remote hosts only)
- `host.ssh.user` (cel, template) - SSH username
- `host.ssh.host` (cel, template) - SSH hostname
- `host.ssh.port` (cel, template) - SSH port
- `host.ssh.key_path` (cel, template) - Path to SSH private key
- `host.ssh.command` (cel, template) - Full SSH command to connect
- `host.snapshots` (cel, template) - List of available snapshots
- `host.is_locked` (cel, template) - Whether the host is currently locked for an operation
- `host.locked_time` (cel, template) - When the host was locked
- `host.plugin` (cel, template) - Plugin-defined fields
- `host.ssh_activity_time` (cel, template) - Last SSH activity time (from host-level activity/ssh file mtime)
- `host.failure_reason` (cel, template) - Reason for failure if the host failed during creation

**Computed and alias fields:**
- `age` (cel) - Seconds since create_time.
- `runtime` (cel) - Alias for runtime_seconds.
- `idle` (cel) - Seconds since the most recent activity across user_activity_time, agent_activity_time, and host.ssh_activity_time (only present when at least one is set).
- `host.provider` (cel, template) - Alias for host.provider_name (the documented short form).
- `project` (cel, template) - Alias for labels.project (mirrors the --project filter flag).

**Dynamic fields:**
- `labels.$KEY` (cel, template) - A specific agent label value (e.g. labels.project).
- `plugin.$PLUGIN_NAME.*` (cel, template) - Plugin-defined agent fields (e.g. plugin.chat_history.messages).
- `host.tags.$KEY` (cel, template) - A specific host label/tag value (e.g. host.tags.env).
- `host.plugin.$PLUGIN_NAME.*` (cel, template) - Host plugin fields (e.g. host.plugin.aws.iam_user).

**Notes:**
- You can use Python-style list slicing for list fields (e.g. `host.snapshots[0]` for the first snapshot, `host.snapshots[:3]` for the first 3).

## See Also

- [mngr create](./create.md) - Create a new agent
- [mngr connect](./connect.md) - Connect to an existing agent
- [mngr destroy](./destroy.md) - Destroy agents
- [mngr help multi_target](../generic/multi_target.md) - Behavior when some agents cannot be accessed
- [mngr help common](../generic/common.md) - Common CLI options for output format, logging, etc.

## Examples

**List all agents**

```bash
$ mngr list
```

**List only running agents**

```bash
$ mngr list --running
```

**List agents on Docker hosts**

```bash
$ mngr list --provider docker
```

**List agents for a project**

```bash
$ mngr list --project mngr
```

**List agents with a specific label**

```bash
$ mngr list --label env=prod
```

**List agents with a specific host label**

```bash
$ mngr list --host-label env=prod
```

**List agents as JSON**

```bash
$ mngr list --format json
```

**Filter with CEL expression**

```bash
$ mngr list --include 'name.contains("prod")'
```

**Sort by name descending**

```bash
$ mngr list --sort 'name desc'
```

**Sort by multiple fields**

```bash
$ mngr list --sort 'state, name asc, create_time desc'
```

**Custom column header**

```bash
$ mngr list --fields name,labels.env --header labels.env=ENV
```
