<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr state

**Synopsis:**

```text
mngr state [TARGET] [--quick] [--fields FIELDS]
```

Show the current state and details of a single agent or host.

Unlike `mngr list`, which enumerates every provider and then filters, `state`
resolves just the one target (querying only its provider) and fetches only it --
so it is cheap even when you have many agents, as long as you know which one you want.

TARGET can be an agent ID (agent-*), host ID (host-*), or an agent/host name.
If TARGET is omitted, it is read from stdin (one line).

For an agent target, the full agent details are shown (the same fields as `mngr list`,
including host information). For a host target, the host details are shown along with
the agents running on it.

Use --quick to report only the lifecycle state (agent + host) without the full detail
fetch -- this skips plugin field generators and is cheaper, useful for scripting.

Output honors --format (human, json, jsonl, or a template like '{name} {state}') and,
in human mode, --fields to choose which fields to display.

**Usage:**

```text
mngr state [OPTIONS] [TARGET]
```
## Arguments

- `TARGET`: The target (optional)

**Options:**

## State options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--quick` | boolean | Report only the lifecycle state (agent + host), skipping the full detail fetch (cheaper). | `False` |
| `--fields` | text | Comma-separated fields to show in human output (same field names as `mngr list`). | None |

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

- [mngr list](./list.md) - List all agents and their current states
- [mngr wait](../secondary/wait.md) - Wait for an agent or host to reach a target state

## Examples

**Show an agent's state and details**

```bash
$ mngr state my-agent
```

**Just the lifecycle state (cheap)**

```bash
$ mngr state my-agent --quick
```

**As JSON**

```bash
$ mngr state my-agent --format json
```

**Only specific fields**

```bash
$ mngr state my-agent --fields state,host.state,idle_seconds
```

**With a template**

```bash
$ mngr state my-agent --format '{name} {state}'
```

**A host's state and its agents**

```bash
$ mngr state host-abc123
```

**Read target from stdin**

```bash
$ echo agent-abc123 | mngr state
```
