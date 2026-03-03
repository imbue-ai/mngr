<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng events

**Synopsis:**

```text
mng events TARGET [EVENT_FILE] [--follow] [--tail N] [--head N]
```

View event files from an agent or host [experimental].

TARGET identifies an agent (by name or ID) or a host (by name or ID).
The command first tries to match TARGET as an agent, then as a host.

If EVENT_FILE is not specified, lists all available event files.
If EVENT_FILE is specified, prints its contents.

In follow mode (--follow), the command uses tail -f for real-time
streaming when the host is online (locally or via SSH). When the host
is offline, it falls back to polling the volume for new content.
Press Ctrl+C to stop.

When listing files, supports custom format templates via --format. Available fields: name, size.

**Usage:**

```text
mng events [OPTIONS] TARGET [EVENT_FILENAME]
```
## Arguments

- `TARGET`: Agent or host name/ID whose events to view
- `EVENT_FILE`: Name of the event file to view (optional; lists files if omitted)

**Options:**

## Display

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--follow`, `--no-follow` | boolean | Continue running and print new messages as they appear | `False` |
| `--tail` | integer range | Print the last N lines of the event file | None |
| `--head` | integer range | Print the first N lines of the event file | None |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided [experimental], fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `--json` | boolean | Alias for --format json | `False` |
| `--jsonl` | boolean | Alias for --format jsonl | `False` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mng/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## See Also

- [mng list](../primary/list.md) - List available agents
- [mng exec](../primary/exec.md) - Execute commands on an agent's host

## Examples

**List available event files for an agent**

```bash
$ mng events my-agent
```

**View a specific event file**

```bash
$ mng events my-agent output.log
```

**View the last 50 lines**

```bash
$ mng events my-agent output.log --tail 50
```

**Follow an event file**

```bash
$ mng events my-agent output.log --follow
```

**List files with custom format template**

```bash
$ mng events my-agent --format '{name}\t{size}'
```
