<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng observe

**Synopsis:**

```text
mng observe [--events-dir DIR]
```

Observe agent state changes across all hosts [experimental].

Continuously monitors agent state across all hosts and writes
events to local JSONL files:

- <events-dir>/events/mng/agents/events.jsonl: individual and full agent state snapshots
- <events-dir>/events/mng/agent_states/events.jsonl: only when the lifecycle state field changes

The observer:
1. Loads base state from event history (if available) to detect state changes since last run
2. Uses 'mng list --stream' to track which hosts are online
3. Streams activity events from each online host
4. When activity is detected, fetches and emits agent state for the affected host
5. Periodically (every 5 minutes) emits a full state snapshot of all agents

Only one instance per output directory can run at a time (enforced via file lock).
Use --events-dir to write events to a different directory, allowing multiple
observers to run simultaneously for different output locations.

Press Ctrl+C to stop.

**Usage:**

```text
mng observe [OPTIONS]
```
## Arguments



**Options:**

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

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--events-dir` | path | Base directory for event output files and lock. Defaults to MNG_HOST_DIR (~/.mng). | None |

## See Also

- [mng list](../primary/list.md) - List available agents
- [mng events](./events.md) - View events from an agent or host

## Examples

**Start observing all agents**

```bash
$ mng observe
```

**Write events to a custom directory**

```bash
$ mng observe --events-dir /path/to/events
```

**Start in quiet mode**

```bash
$ mng observe --quiet
```
