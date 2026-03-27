<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr transcript

**Synopsis:**

```text
mngr transcript TARGET [--role ROLE] [--tail N] [--head N] [--format human|json|jsonl]
```

View the message transcript for an agent.

View the common transcript for an agent. The transcript contains
user messages, assistant messages, and tool call/result summaries in a
common, agent-agnostic format.

The command automatically finds the correct transcript file regardless
of the agent type (e.g. claude, codex).

Use --role to filter by message role (user, assistant, tool). This
option is repeatable to include multiple roles.

Use --format to control output:
  - human (default): nicely formatted, readable output
  - jsonl: raw JSONL, one event per line (for piping)
  - json: full JSON array (for programmatic use)

**Usage:**

```text
mngr transcript [OPTIONS] TARGET
```
## Arguments

- `TARGET`: Agent name or ID whose transcript to view

**Options:**

## Filtering

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--role` | text | Only show messages with this role (repeatable; e.g. user, assistant, tool) | None |

## Display

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--tail` | integer range | Show only the last N transcript events | None |
| `--head` | integer range | Show only the first N transcript events | None |

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
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## See Also

- [mngr events](./events.md) - View all events from an agent or host
- [mngr message](./message.md) - Send a message to an agent

## Examples

**View full transcript**

```bash
$ mngr transcript my-agent
```

**View only user messages**

```bash
$ mngr transcript my-agent --role user
```

**View user and assistant messages**

```bash
$ mngr transcript my-agent --role user --role assistant
```

**View last 20 events**

```bash
$ mngr transcript my-agent --tail 20
```

**Output as JSONL for piping**

```bash
$ mngr transcript my-agent --format jsonl
```

**Output as JSON**

```bash
$ mngr transcript my-agent --format json
```
