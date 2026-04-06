<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr events

**Synopsis:**

```text
mngr events TARGET [SOURCES...] [--source SOURCE] [--include CEL] [--exclude CEL] [--follow] [--tail N] [--head N]
```

View events from an agent or host.

TARGET identifies an agent (by name or ID) or a host (by name or ID).
The command first tries to match TARGET as an agent, then as a host.

Streams all events from all sources in date-sorted order. Use --source
or positional SOURCES arguments to restrict which event sources to include.
Use --include and --exclude to further restrict events via CEL expressions.
All --include filters must match for an event to be included, and events
matching any --exclude filter are dropped. Use --follow to continuously
stream new events.

In follow mode (--follow), the command polls for new events. When the host
is online, it reads files directly. When offline, it falls back to polling
the volume. The command handles online/offline transitions automatically.
Press Ctrl+C to stop.

**Usage:**

```text
mngr events [OPTIONS] TARGET [SOURCES]...
```
## Arguments

- `TARGET`: Agent or host name/ID whose events to view
- `SOURCES`: Event sources to include (optional; includes all sources if omitted). These are paths relative to the target's events/ directory (e.g. 'messages', 'logs/mngr').

**Options:**

## Display

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--follow`, `--no-follow` | boolean | Continue running and print new events as they appear | `False` |
| `--tail` | integer range | Print the last N events | None |
| `--head` | integer range | Print the first N events | None |

## Filtering

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--source` | text | Event source to include, relative to events/ (e.g. 'messages', 'logs/mngr'). Can be repeated. | None |
| `--include` | text | CEL expression that events must match to be included (repeatable, all must match). | None |
| `--exclude` | text | CEL expression; events matching any exclude filter are dropped (repeatable). | None |

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

## See Also

- [mngr list](../primary/list.md) - List available agents
- [mngr exec](../primary/exec.md) - Execute commands on an agent's host

## Examples

**Stream all events for an agent**

```bash
$ mngr events my-agent
```

**Stream only message events**

```bash
$ mngr events my-agent messages
```

**Stream events from multiple sources**

```bash
$ mngr events my-agent messages logs/mngr
```

**Same thing using --source**

```bash
$ mngr events my-agent --source messages --source logs/mngr
```

**Include only user messages**

```bash
$ mngr events my-agent --include 'source == "messages"' --include 'data.role == "user"'
```

**Exclude log events**

```bash
$ mngr events my-agent --exclude 'source.startsWith("logs/")'
```

**View last 100 events**

```bash
$ mngr events my-agent --tail 100
```

**Follow all events in real-time**

```bash
$ mngr events my-agent --follow
```
