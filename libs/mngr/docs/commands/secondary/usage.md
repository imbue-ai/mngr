<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr usage

**Synopsis:**

```text
mngr usage [OPTIONS]
```

Show rolling-window usage / quota data from agent statusline events.

Reports rolling-window usage / quota data captured by an agent's
statusline.

Agent-agnostic and host-agnostic: enumerates matching agents via
``list_agents`` (same machinery and filter vocabulary as ``mngr list``) and
reads each agent's ``events/<source>/rate_limits/events.jsonl`` via the
events API. Local and remote agents are read uniformly; the writer plugin
chooses the ``<source>`` segment.

**Usage:**

```text
mngr usage [OPTIONS] COMMAND [ARGS]...
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
| `--provider` | text | Show only agents from the given provider(s) (repeatable, e.g. --provider local) | None |

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
| `-S`, `--setting` | text | Override a config setting for this invocation (KEY=VALUE, dot-separated paths) [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--max-age` | text | Stale-warning threshold (e.g. '300', '5m', '2h'). Default: from plugin config. | None |

## mngr usage wait

**Usage:**

```text
mngr usage wait [OPTIONS]
```
**Options:**

## Predicate

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--until` | text | CEL expression that must evaluate true for some source to win the wait [repeatable, all must match]. The CEL context is the per-source dict from `mngr usage --format json` (see help description for shape). | None |
| `--source` | text | Only consider these writer sources (e.g. 'claude'). When omitted, any source may satisfy the predicate [repeatable]. | None |

## Wait options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--timeout` | text | Maximum time to wait (e.g. '30s', '5m', '1h'). Default: wait forever. | None |
| `--interval` | text | Poll interval (e.g. '15s', '1m'). The usage snapshot is rebuilt every interval. Default of 30s suits multi-hour windows; tighten for short-window predicates. | `30s` |

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
| `--provider` | text | Restrict to agents from the given provider(s) (repeatable, e.g. --provider local). | None |

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
| `-S`, `--setting` | text | Override a config setting for this invocation (KEY=VALUE, dot-separated paths) [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Examples

**Show current usage**

```bash
$ mngr usage
```

**Local agents only**

```bash
$ mngr usage --local
```

**Specific providers**

```bash
$ mngr usage --provider local --provider modal
```

**Treat the snapshot as stale after 60s (warning only)**

```bash
$ mngr usage --max-age 60
```

**Machine-readable output**

```bash
$ mngr usage --format json
```

**Custom format template**

```bash
$ mngr usage --format '{five_hour.used_percentage}/{seven_day.used_percentage}'
```
