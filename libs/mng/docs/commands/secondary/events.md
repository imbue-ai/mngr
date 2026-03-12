<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng events

**Synopsis:**

```text
mng events TARGET [EVENT_FILE] [--filter CEL] [--follow] [--tail N] [--head N]
```

View events from an agent or host.

TARGET identifies an agent (by name or ID) or a host (by name or ID).
The command first tries to match TARGET as an agent, then as a host.

If EVENT_FILE is not specified, streams all events from all sources in
date-sorted order. Use --filter to restrict which events are included
via a CEL expression. Use --follow to continuously stream new events.

If EVENT_FILE is specified, prints its contents directly.

In follow mode (--follow), the command polls for new events. When the host
is online, it reads files directly. When offline, it falls back to polling
the volume. The command handles online/offline transitions automatically.
Press Ctrl+C to stop.

**Usage:**

```text
mng events [OPTIONS] TARGET [EVENT_FILENAME]
```
## Arguments

- `TARGET`: Agent or host name/ID whose events to view
- `EVENT_FILE`: Name of a specific event file to view (optional; streams all events if omitted)

**Options:**

## Display

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--follow`, `--no-follow` | boolean | Continue running and print new events as they appear | `False` |
| `--tail` | integer range | Print the last N events (or lines when viewing a specific file) | None |
| `--head` | integer range | Print the first N events (or lines when viewing a specific file) | None |

## Filtering

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--filter` | text | CEL expression to filter which events to include (e.g. 'source == "messages"') | None |

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

## See Also

- [mng list](../primary/list.md) - List available agents
- [mng exec](../primary/exec.md) - Execute commands on an agent's host

## Examples

**Stream all events for an agent**

```bash
$ mng events my-agent
```

**Stream only message events**

```bash
$ mng events my-agent --filter 'source == "messages"'
```

**View last 100 events**

```bash
$ mng events my-agent --tail 100
```

**Follow all events in real-time**

```bash
$ mng events my-agent --follow
```

**View a specific event file**

```bash
$ mng events my-agent messages/events.jsonl
```

**Follow a specific event file**

```bash
$ mng events my-agent messages/events.jsonl --follow
```
