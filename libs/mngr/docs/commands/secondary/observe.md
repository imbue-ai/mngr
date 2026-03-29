<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr observe

**Synopsis:**

```text
mngr observe [--events-dir DIR] [--discovery-only]
```

Observe agent state changes across all hosts [experimental].

Continuously monitors agent state across all hosts and writes
events to local JSONL files:

- <events-dir>/events/mngr/agents/events.jsonl: individual and full agent state snapshots
- <events-dir>/events/mngr/agent_states/events.jsonl: only when the lifecycle state field changes

The observer:
1. Loads base state from event history (if available) to detect state changes since last run
2. Runs host discovery to track which hosts are online
3. Streams activity events from each online host
4. When activity is detected, fetches and emits agent state for the affected host
5. Periodically (every 5 minutes) emits a full state snapshot of all agents

Only one instance per output directory can run at a time (enforced via file lock).
Use --events-dir to write events to a different directory, allowing multiple
observers to run simultaneously for different output locations.

With --discovery-only, only the host/agent discovery stream is emitted as JSONL
to stdout. This is useful for programmatically tracking which agents and hosts
exist without the full observe overhead.

Press Ctrl+C to stop.

**Usage:**

```text
mngr observe [OPTIONS]
```
## Arguments



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

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--events-dir` | path | Base directory for event output files and lock. Defaults to MNGR_HOST_DIR (~/.mngr). | None |
| `--discovery-only` | boolean | Stream only discovery events as JSONL (hosts and agents discovered/destroyed). Outputs a full snapshot, then tails the event file for updates. Periodically re-polls to catch any missed changes. Does not start activity streams or emit agent state events. | `False` |
| `--on-error` | choice (`abort` &#x7C; `continue`) | What to do when errors occur: abort (stop immediately) or continue (keep going) | `abort` |

## See Also

- [mngr list](../primary/list.md) - List available agents
- [mngr events](./events.md) - View events from an agent or host

## Examples

**Start observing all agents**

```bash
$ mngr observe
```

**Write events to a custom directory**

```bash
$ mngr observe --events-dir /path/to/events
```

**Start in quiet mode**

```bash
$ mngr observe --quiet
```

**Stream only discovery events**

```bash
$ mngr observe --discovery-only
```
