<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng wait

**Synopsis:**

```text
mng wait [TARGET] [STATE ...] [--state STATE ...] [--timeout DURATION] [--interval DURATION]
```

Wait for an agent or host to reach a target state.

Wait for an agent or host to transition to one of the specified states.

TARGET can be an agent ID (agent-*), host ID (host-*), or an agent/host name.
If TARGET is omitted, it is read from stdin (one line, must be an ID like agent-* or host-*).

States can be provided as positional arguments after TARGET, via the repeatable --state option, or both.
Valid states include all agent lifecycle states (STOPPED, RUNNING, WAITING, REPLACED, DONE) and
all host states (BUILDING, STARTING, RUNNING, STOPPING, STOPPED, PAUSED, CRASHED, FAILED, DESTROYED, UNAUTHENTICATED).

If no states are specified, waits for any terminal state (the target stops running).

When watching an agent, both agent and host states are tracked:
- STOPPED counts if either the agent or host is stopped
- RUNNING only counts if the agent itself is running (not the host)
- Host-specific states (CRASHED, PAUSED, etc.) are matched against the host

Exit codes:
  0 - Target reached one of the requested states
  1 - Error
  2 - Timeout expired

**Usage:**

```text
mng wait [OPTIONS] [TARGET] [STATES]...
```
## Arguments

- `TARGET`: The target (optional)
- `STATES`: The states (optional)

**Options:**

## Wait options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--state` | text | State to wait for [repeatable]. Can also be passed as positional args after TARGET. | None |
| `--timeout` | text | Maximum time to wait (e.g. '30s', '5m', '1h'). Default: wait forever. | None |
| `--interval` | text | Poll interval (e.g. '5s', '1m'). Default: 5s. | `5s` |

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

- [mng list](../primary/list.md) - List agents and their current states

## Examples

**Wait for an agent to finish**

```bash
$ mng wait my-agent DONE
```

**Wait for any terminal state**

```bash
$ mng wait agent-abc123
```

**Wait for agent to enter WAITING**

```bash
$ mng wait my-agent WAITING
```

**Wait with timeout**

```bash
$ mng wait my-agent DONE --timeout 5m
```

**Wait for host to stop**

```bash
$ mng wait host-xyz789 STOPPED
```

**Read target from stdin**

```bash
$ echo agent-abc123 | mng wait
```

**Multiple states**

```bash
$ mng wait my-agent --state WAITING --state DONE
```
