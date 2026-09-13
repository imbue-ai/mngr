<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr transcript

**Synopsis:**

```text
mngr transcript TARGET [--role ROLE] [--tail N] [--head N] [--full] [--format human|json|jsonl|atif] [--output PATH]
```

View the message transcript for an agent.

View the common transcript for an agent. The transcript contains
user turns, agent turns, and tool results in a common, agent-agnostic format.

The command automatically finds the correct transcript file regardless
of the agent type (e.g. claude, codex).

Use --role to filter by message role (user, agent, system, tool). This
option is repeatable to include multiple roles.

Human output truncates long tool inputs, tool outputs, and thinking for
readability; pass --full to see them untruncated. Only the display is
truncated -- the underlying stream (and --format json/jsonl/atif) always
carries the complete text.

Use --format to control output:
  - human (default): nicely formatted, readable output
  - jsonl: raw JSONL, one event per line (for piping)
  - json: full JSON array (for programmatic use)
  - atif: a single validated ATIF trajectory document assembled from the
    stream (Agent Trajectory Interchange Format; embeds resolvable
    subagent trajectories). Use --output PATH to write it to a file.

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
| `--role` | text | Only show messages with this role (repeatable; user, agent, system, tool) | None |

## Display

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--tail` | integer range | Show only the last N transcript events | None |
| `--head` | integer range | Show only the first N transcript events | None |
| `--output` | path | Write the built ATIF document to this file instead of stdout (only with --format atif) | None |
| `--full` | boolean | Disable display-time truncation of tool inputs/outputs and thinking (human output only) | `False` |

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

## See Also

- [mngr event](./event.md) - View all events from an agent or host
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

**View user and agent messages**

```bash
$ mngr transcript my-agent --role user --role agent
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

**Build a full ATIF trajectory document**

```bash
$ mngr transcript my-agent --format atif
```
