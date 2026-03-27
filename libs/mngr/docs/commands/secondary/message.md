<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr message

**Synopsis:**

```text
mngr [message|msg] [AGENTS...|-] [--agent <AGENT>] [--all] [-m <MESSAGE>]
```

Send a message to one or more agents.

Agent IDs can be specified as positional arguments for convenience. The
message is sent to the agent's stdin.

If no message is specified with --message, reads from stdin (if not a tty)
or opens an editor (if interactive).

Alias: msg

**Usage:**

```text
mngr message [OPTIONS] [AGENTS]...
```
## Arguments

- `AGENTS`: The agents (optional)

**Options:**

## Target Selection

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--agent` | text | Agent name or ID to send message to (can be specified multiple times) | None |
| `-a`, `--all`, `--all-agents` | boolean | Send message to all agents | `False` |
| `--include` | text | Include agents matching CEL expression (repeatable) | None |
| `--exclude` | text | Exclude agents matching CEL expression (repeatable) | None |
| `--start`, `--no-start` | boolean | Automatically start offline hosts and stopped agents before sending | `False` |

## Message Content

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `-m`, `--message` | text | The message content to send | None |

## Error Handling

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--on-error` | choice (`abort` &#x7C; `continue`) | What to do when errors occur: abort (stop immediately) or continue (keep going) | `continue` |
| `--provider` | text | Message only agents using specified provider (repeatable) | None |

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

## Related Documentation

- [Multi-target Options](../generic/multi_target.md) - Behavior when some agents fail to receive the message

## See Also

- [mngr connect](../primary/connect.md) - Connect to an agent interactively
- [mngr list](../primary/list.md) - List available agents

## Examples

**Send a message to an agent**

```bash
$ mngr message my-agent --message "Hello"
```

**Send to multiple agents**

```bash
$ mngr message agent1 agent2 --message "Hello to all"
```

**Send to all agents**

```bash
$ mngr message --all --message "Hello everyone"
```

**Pipe message from stdin**

```bash
$ echo "Hello" | mngr message my-agent
```

**Use --agent flag (repeatable)**

```bash
$ mngr message --agent my-agent --agent another-agent --message "Hello"
```
