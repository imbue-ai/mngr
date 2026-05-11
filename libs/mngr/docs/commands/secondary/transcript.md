<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr transcript

**Synopsis:**

```text
mngr transcript [TARGET] [--role ROLE] [--tail N | --head N | --turn N | --last-completed-turn | --count-turns | --list-turns] [--format human|json|jsonl]
```

View the message transcript for an agent.

View the common transcript for an agent. The transcript contains
user messages, assistant messages, and tool call/result summaries in a
common, agent-agnostic format.

The command automatically finds the correct transcript file regardless
of the agent type (e.g. claude, codex). If TARGET is omitted, the
command resolves the current agent from the `MNGR_AGENT_ID` environment
variable that mngr exports into every agent's shell.

Use --role to filter by message role (user, assistant, tool). This
option is repeatable to include multiple roles.

Turn-aware options operate on conversational turns, where each
`user_message` event marks a turn boundary. They are mutually exclusive
with each other and with --head / --tail:
  - --turn N: extract a single turn. Positive N is 1-indexed from the
    start; negative N counts from the end (--turn -1 = last/in-progress,
    --turn -2 = previous completed).
  - --last-completed-turn: shortcut for --turn -2.
  - --count-turns: print just the turn count and exit.
  - --list-turns: summary table of turn boundaries (respects --format).

Use --format to control output:
  - human (default): nicely formatted, readable output
  - jsonl: raw JSONL, one event per line (for piping)
  - json: full JSON array (for programmatic use)

**Usage:**

```text
mngr transcript [OPTIONS] [TARGET]
```
## Arguments

- `TARGET`: Agent name or ID whose transcript to view. Optional when the command runs inside an agent context that exports `MNGR_AGENT_ID`.

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

## Turns

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--turn` | integer | Extract a single turn by 1-indexed position. Negative indices count from the end (--turn -1 is the last/in-progress turn, --turn -2 is the previous completed turn). A 'turn' is the slice from one user_message (inclusive) up to the next user_message (exclusive). | None |
| `--last-completed-turn` | boolean | Extract the most recent completed turn (equivalent to --turn -2). | `False` |
| `--count-turns` | boolean | Print the number of turns in the transcript and exit. | `False` |
| `--list-turns` | boolean | List each turn's number, timestamp, and a content preview instead of the events themselves. | `False` |

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

**Count turns in the transcript**

```bash
$ mngr transcript my-agent --count-turns
```

**Extract the previous completed turn (from inside an agent)**

```bash
$ mngr transcript --last-completed-turn --format jsonl
```

**Extract the second turn**

```bash
$ mngr transcript my-agent --turn 2 --format jsonl
```

**List all turn boundaries**

```bash
$ mngr transcript my-agent --list-turns
```
